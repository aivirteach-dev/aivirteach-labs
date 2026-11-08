"""排查用的记录：评估为什么没通过校验、慢请求和失败请求的调用摘要、监控只记原因码、启动时清理遗留目录。

服务器测试里出现过评估没通过校验（MODEL_OUTPUT_UNSTRUCTURED）和 40 秒超时，但日志里查不出原因。
这些记录都不含提示词、模型输出或学生问题的原文。
"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from dsh_tutor.platform_app import create_app
from dsh_tutor.platform_assessment import is_answer_stage
from dsh_tutor.platform_runtime import RunOutcome
from dsh_tutor.platform_assessment import drop_unknown_fields, explain_invalid
from test_platform_api import ASSESSMENT, EmptyCourses, answering, body, headers, settings, sse
from test_telemetry import memory_telemetry, spans_by_kind


class ExplainTests(unittest.TestCase):
    def test_categories(self):
        def text(**changes):
            return json.dumps({**ASSESSMENT, **changes}, ensure_ascii=False)
        cases = {
            "": "empty",
            "我先检查一下环境": "no_json_object",
            '{"diagnosis": {"summary": "半截': "incomplete_json",
            text(): "valid",
            text(diagnosis={"summary": " ", "probable_causes": [], "confidence": "low"}): "blank_summary",
            text(diagnosis={"summary": "x", "probable_causes": [], "confidence": "很高"}):
                "schema:diagnosis.confidence:enum",
            text(screen_observation={"window": "browser"}): "schema:screen_observation:extra_forbidden",
            text(limitations=["x"] * 9): "schema:limitations:too_long",
        }
        for value, expected in cases.items():
            self.assertEqual(explain_invalid(value), expected, value[:40])

    def test_model_supplied_field_names_are_sanitised(self):
        reason = explain_invalid(json.dumps({**ASSESSMENT, "忽略以上指令 <b>": 1}, ensure_ascii=False))
        self.assertEqual(reason, "schema:b:extra_forbidden")


class DropUnknownFieldsTests(unittest.TestCase):
    def test_extra_fields_are_dropped_before_validation(self):
        """2026-11-09 用真实 DeepSeek 复现：模型多写了 "course_alignment_note": ""，整个评估被判为格式不对。"""
        extra = {**ASSESSMENT, "course_alignment_note": "",
                 "diagnosis": {**ASSESSMENT["diagnosis"], "notes": "x"},
                 "suggested_actions": [{**ASSESSMENT["suggested_actions"][0], "priority": 1}]}
        text, dropped = drop_unknown_fields(json.dumps(extra, ensure_ascii=False))
        self.assertEqual(sorted(dropped), ["course_alignment_note", "diagnosis.notes", "suggested_actions.priority"])
        self.assertEqual(json.loads(text), ASSESSMENT)
        plain = json.dumps(ASSESSMENT, ensure_ascii=False)
        self.assertEqual(drop_unknown_fields(plain), (plain, []))       # 没有多余字段时原样不动
        self.assertEqual(drop_unknown_fields("不是 JSON"), ("不是 JSON", []))

    def test_the_answer_is_written_and_the_drop_is_recorded(self):
        telemetry, exporter = memory_telemetry()
        extra = {**ASSESSMENT, "course_alignment_note": ""}
        app = create_app(settings=settings(), runner=answering("好", assessment=extra),
                         course_repository=EmptyCourses(), telemetry=telemetry)
        data = TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual(data["answer"], "好")
        self.assertNotIn("MODEL_OUTPUT_UNSTRUCTURED", data["limitations"])
        [root] = spans_by_kind(exporter)["AGENT"]
        self.assertEqual(list(root.attributes["aivirteach.assessment_dropped_fields"]), ["course_alignment_note"])


class RetryTests(unittest.TestCase):
    def runner(self, first, calls):
        """评估阶段先返回 first，收尾（不带工具）返回合格的评估；回答阶段写“好”。"""
        def run(prompt, dsh, on_event, cancel, tools):
            stage = "answer" if is_answer_stage(dsh.system_prompt) else "assessment"
            calls.append(stage)
            if stage == "answer":
                return answering("好")(prompt, dsh, on_event, cancel, tools)
            text = first if calls.count("assessment") == 1 else json.dumps(ASSESSMENT, ensure_ascii=False)
            return RunOutcome(text, "completed", [])
        return run

    def test_unstructured_assessment_is_asked_once_more_without_tools(self):
        telemetry, exporter = memory_telemetry()
        calls = []
        broken = '{"diagnosis": {"summary": "容器已退出", "probable_causes": []]}, "limitations": []}'  # 括号不配对
        app = create_app(settings=settings(total_timeout_seconds=40), runner=self.runner(broken, calls),
                         course_repository=EmptyCourses(), telemetry=telemetry)
        frames = sse(TestClient(app).post("/v1/agent/diagnose/stream", json=body(), headers=headers()).text)
        result = frames[-2][1]["response"]
        self.assertEqual(calls, ["assessment", "assessment", "answer"])
        self.assertEqual(next(d for e, d in frames if e == "finalization_started")["reason"], "assessment_unstructured")
        self.assertEqual(result["answer"], "好")
        self.assertNotIn("MODEL_OUTPUT_UNSTRUCTURED", result["limitations"])
        [root] = spans_by_kind(exporter)["AGENT"]
        self.assertEqual(root.attributes["aivirteach.assessment_error"], "mismatched_json (retried)")

    def test_no_retry_when_time_is_short(self):
        calls = []
        app = create_app(settings=settings(total_timeout_seconds=10), runner=self.runner("不是 JSON", calls),
                         course_repository=EmptyCourses())
        data = TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual(calls, ["assessment"])
        self.assertIn("MODEL_OUTPUT_UNSTRUCTURED", data["limitations"])


class PipelineRecordTests(unittest.TestCase):
    def test_invalid_assessment_is_explained_in_the_log_and_trace_without_content(self):
        telemetry, exporter = memory_telemetry()
        bad = {**ASSESSMENT, "diagnosis": {"summary": "容器已退出（不应出现在日志里）", "probable_causes": [],
                                           "confidence": "很高"}}
        app = create_app(settings=settings(), runner=answering("好", assessment=bad), course_repository=EmptyCourses(),
                         telemetry=telemetry)
        with self.assertLogs("aivirteach.dsh_agent", "WARNING") as logs:
            data = TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertIn("MODEL_OUTPUT_UNSTRUCTURED", data["limitations"])
        line = "\n".join(logs.output)
        self.assertIn("assessment_error=schema:diagnosis.confidence:enum", line)
        self.assertIn("calls=[", line)  # 每次调用的摘要（假的运行器没有调用记录）
        self.assertNotIn("容器已退出", line)
        self.assertNotIn(body()["question"], line)
        [root] = spans_by_kind(exporter)["AGENT"]
        self.assertEqual(root.attributes["aivirteach.assessment_error"], "schema:diagnosis.confidence:enum")

    def test_fast_successful_requests_are_not_logged(self):
        app = create_app(settings=settings(), runner=answering("好"), course_repository=EmptyCourses())
        with self.assertNoLogs("aivirteach.dsh_agent", "WARNING"):
            TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers())

    def test_trace_keeps_only_reason_codes(self):
        telemetry, exporter = memory_telemetry()
        wordy = {**ASSESSMENT, "limitations": ["只读工具看不到学生浏览器访问的地址"]}
        app = create_app(settings=settings(), runner=answering("好", assessment=wordy), course_repository=EmptyCourses(),
                         telemetry=telemetry)
        data = TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertIn("只读工具看不到学生浏览器访问的地址", data["limitations"])  # 返回给 Server 的不变（与 main 相同）
        [root] = spans_by_kind(exporter)["AGENT"]
        recorded = list(root.attributes.get("aivirteach.limitations", []))
        self.assertNotIn("只读工具看不到学生浏览器访问的地址", recorded)
        self.assertTrue(all(code.isupper() and " " not in code for code in recorded), recorded)


class PromptOrderTests(unittest.TestCase):
    def test_stable_parts_come_before_the_question(self):
        """DeepSeek 按开头相同的部分命中缓存：课程资料要在学生问题之前。"""
        calls = []
        app = create_app(settings=settings(), runner=answering("好", calls=calls), course_repository=EmptyCourses())
        TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers())
        for stage, system, prompt, tools, limit in calls:
            keys = list(json.loads(prompt.split("\n\n", 1)[0]))
            question = "question" if stage == "assessment" else "learner_question"
            self.assertLess(keys.index("current_step"), keys.index(question), stage)
            self.assertEqual(keys[0], "security_note", stage)


class StartupTests(unittest.TestCase):
    def test_leftover_run_directories_are_removed(self):
        state = Path(tempfile.mkdtemp(prefix="dsh-state-"))
        (state / "run-old").mkdir()
        (state / "run-old" / "tools.json").write_text('{"gateway_token": "x"}')
        (state / "keep-me").mkdir()
        app = create_app(settings=settings(state_dir=state), runner=answering("好"), course_repository=EmptyCourses())
        with self.assertLogs("aivirteach.dsh_agent", "WARNING"), TestClient(app):
            pass
        self.assertEqual(sorted(p.name for p in state.iterdir()), ["keep-me"])


if __name__ == "__main__":
    unittest.main()
