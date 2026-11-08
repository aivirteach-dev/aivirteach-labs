"""画面接入诊断流程：提示词里的画面描述、证据引用、回答末尾的说明，以及带截图时预算和事件与不带时相同。

看图模型和对话模型都是假的（httpx.MockTransport / answering），不连真实模型。
"""
import json
import unittest

from fastapi.testclient import TestClient

from dsh_tutor import platform_screen as screen
from dsh_tutor import platform_vision as vision
from dsh_tutor.platform_app import create_app
from dsh_tutor.platform_runtime import RunOutcome
from dsh_tutor.platform_assessment import validate
from dsh_tutor.platform_screen_flow import (SCREEN_RULES, answer_note, drop_screen_reference, position_words,
                                            with_screen)
from dsh_tutor.platform_vision import Perception
from dsh_tutor.platform_screen import Highlight
from test_platform_api import ASSESSMENT, EmptyCourses, answering, body, headers, settings, sse
from test_screen import capture
from test_vision import FakeVision, analyzer, perception

UI_QUESTION = "我找不到 Create Workflow 按钮在哪里"
ENV_QUESTION = "n8n 打不开，浏览器显示连接被拒绝，为什么？"
NOTE_UNUSED = "（这次没能查看你发来的虚拟机画面，以上回答没有参考画面。）"
NOTE_SENSITIVE = "（你发来的画面上可能有密钥或密码，我没有使用这张截图。请先关闭显示密钥的窗口，再重新发送画面。）"


def sample_perception(**overrides):
    values = dict(readable=True, window="browser", app="Firefox", page="n8n - Overview",
                  summary="学生在 n8n 首页。", visible_errors=["Bad request"],
                  elements=[Highlight(label="Create Workflow", x=0.82, y=0.04, width=0.12, height=0.05)],
                  zoom=None, contains_sensitive=False)
    values.update(overrides)
    return Perception(**values)


class UnitTests(unittest.TestCase):
    def test_positions(self):
        self.assertEqual(position_words(0.82, 0.04, 0.12, 0.05, "zh-CN"), "右上")
        self.assertEqual(position_words(0.45, 0.89, 0.1, 0.04, "zh-CN"), "底部中间")
        self.assertEqual(position_words(0.05, 0.45, 0.1, 0.1, "en"), "middle left")

    def test_screen_block_in_prompts(self):
        system, user = with_screen(("系统", "用户"), sample_perception(), "zh-CN")
        self.assertTrue(system.startswith("系统") and system.endswith(SCREEN_RULES))
        block = json.loads(user.split("\n\n", 1)[1])
        observation = block["screen_observation"]
        self.assertEqual((observation["window"], observation["summary"], observation["visible_errors"]),
                         ("browser", "学生在 n8n 首页。", ["Bad request"]))
        self.assertEqual(observation["elements"], [{"label": "Create Workflow", "position": "右上"}])
        self.assertIn("untrusted", block["security_note"])

    def test_screen_reference_is_removed_before_main_validation(self):
        evidence = [{"id": "obs-001", "tool": "get_vm_status", "summary": "s", "captured_at": "2026-11-07T00:00:00Z"}]
        cited = json.dumps({**ASSESSMENT, "evidence_ids": ["screen-1", "obs-001"]}, ensure_ascii=False)
        assessment, selected, limitations, partial = validate(cited, evidence, [], False)
        self.assertIsNone(assessment)  # 不处理的话 main 的校验会判为引用了不存在的证据
        cleaned = drop_screen_reference(cited)
        assessment, selected, limitations, partial = validate(cleaned, evidence, [], False)
        self.assertEqual((assessment.evidence_ids, partial), (["obs-001"], False))
        plain = json.dumps(ASSESSMENT, ensure_ascii=False)
        self.assertEqual(drop_screen_reference(plain), plain)  # 没有引用时原样不动
        self.assertEqual(drop_screen_reference("不是 JSON"), "不是 JSON")
        self.assertIsNone(drop_screen_reference(None))

    def test_answer_notes(self):
        def note(**overrides):
            values = dict(screen_attached=True, screen_used=False, screen_codes=[], language="zh-CN")
            values.update(overrides)
            return answer_note(**values)
        self.assertEqual(note(screen_used=True), "")
        self.assertEqual(note(screen_codes=[vision.SCREEN_SENSITIVE_CONTENT]), NOTE_SENSITIVE)
        self.assertIn("次数暂时用完", note(screen_codes=[vision.SCREEN_RATE_LIMITED]))
        for code in (screen.SCREEN_TOO_LARGE, screen.SCREEN_NOT_ANALYZED, vision.SCREEN_ANALYSIS_TIMEOUT):
            self.assertEqual(note(screen_codes=[code]), NOTE_UNUSED, code)
        self.assertEqual(note(screen_attached=False), "")  # 不带截图时不加任何说明
        self.assertTrue(note(screen_codes=[screen.SCREEN_TOO_LARGE], language="en").startswith("(I couldn't"))


class FlowTests(unittest.TestCase):
    def run_flow(self, question, *, screen_attached=True, vision_script=(), assessment=None, runner=None):
        calls = []
        tool = analyzer(FakeVision(*vision_script), api_key_env=None)
        client = TestClient(create_app(
            settings=settings(model_provider="openai_compatible", model_api_key_set=True, total_timeout_seconds=40),
            runner=runner(calls) if runner else answering("在右上角。", calls=calls, assessment=assessment),
            course_repository=EmptyCourses(), screen_analyzer=tool))
        payload = body(question=question, **({"screen": capture().model_dump(mode="json")} if screen_attached else {}))
        frames = sse(client.post("/v1/agent/diagnose/stream", json=payload, headers=headers()).text)
        return frames, calls, frames[-2][1]["response"]

    def test_screen_goes_into_the_assessment_with_the_normal_budget(self):
        for question in (UI_QUESTION, ENV_QUESTION):
            frames, calls, result = self.run_flow(question)
            stage, system, prompt, tools, max_calls = calls[0]
            self.assertEqual((stage, max_calls), ("assessment", 4), question)  # 和不带截图相同的预算
            self.assertIn("screen_observation", prompt); self.assertIn("右上", prompt)
            self.assertIn(SCREEN_RULES.strip()[:20], system)
            self.assertNotIn(capture().data[:60], prompt)  # 提示词里只有文字描述，没有图片
            self.assertNotIn("screen_observation", calls[1][2])  # 回答阶段只看校验过的评估（与 main 相同）
            self.assertEqual((result["status"], result["answer"]), ("completed", "在右上角。"))
            self.assertEqual(result["screen_findings"]["highlights"][0]["label"], "Create Workflow")

    def test_with_and_without_screen_send_the_same_events(self):
        with_screen_frames, _, _ = self.run_flow(UI_QUESTION)
        plain_frames, calls, result = self.run_flow(UI_QUESTION, screen_attached=False)
        self.assertEqual([e for e, _ in with_screen_frames], [e for e, _ in plain_frames])
        self.assertNotIn("screen_observation", calls[0][2])
        self.assertEqual(result["answer"], "在右上角。")  # 不带截图时没有任何提示语

    def test_screen_reference_does_not_invalidate_the_assessment(self):
        cited = {**ASSESSMENT, "evidence_ids": ["screen-1"]}
        frames, calls, result = self.run_flow(UI_QUESTION, assessment=cited)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("MODEL_REFERENCED_UNKNOWN_EVIDENCE", result["limitations"])

    def test_finalization_also_sees_the_screen(self):
        def exhausted(calls):
            def runner(prompt, dsh, on_event, cancel, tools):
                if not calls:  # 评估阶段：检查次数用完，没有给出评估
                    calls.append(("assessment", dsh.system_prompt, prompt, tools, dsh.max_model_calls))
                    return RunOutcome("", None, [], "MODEL_BUDGET_EXHAUSTED", budget_stop="model_calls")
                return answering("在右上角。", calls=calls)(prompt, dsh, on_event, cancel, tools)
            return runner
        frames, calls, result = self.run_flow(UI_QUESTION, runner=exhausted)
        self.assertIn("finalization_started", [e for e, _ in frames])
        self.assertEqual([c[0] for c in calls], ["assessment", "assessment", "answer"])  # 第二个是收尾
        self.assertIn("screen_observation", calls[1][2])
        self.assertEqual(result["answer"], "在右上角。")

    def test_sensitive_screen(self):
        frames, calls, result = self.run_flow(UI_QUESTION, vision_script=[perception(contains_sensitive=True)])
        self.assertNotIn("screen_observation", calls[0][2])  # 不使用这张截图的内容
        self.assertEqual(result["answer"], "在右上角。\n\n" + NOTE_SENSITIVE)
        self.assertIn(vision.SCREEN_SENSITIVE_CONTENT, result["limitations"])
        self.assertNotIn("screen_findings", result)

    def test_note_is_added_to_the_fallback_answer(self):
        frames, calls, result = self.run_flow(UI_QUESTION, vision_script=["不是 JSON"], assessment="也不是 JSON")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["answer"].endswith(NOTE_UNUSED))
        self.assertIn(vision.SCREEN_ANALYSIS_INVALID, result["limitations"])


if __name__ == "__main__":
    unittest.main()
