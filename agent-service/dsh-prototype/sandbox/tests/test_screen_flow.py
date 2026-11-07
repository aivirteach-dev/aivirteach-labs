"""画面接入诊断流程（阶段 3）：问题分类、提示词里的画面描述、评估调用上限、证据引用、回答末尾的说明和事件。

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
from dsh_tutor.platform_screen_flow import (ENVIRONMENT, OTHER, SCREEN_RULES, UI, answer_note, classify_question,
                                            drop_screen_reference, position_words, with_screen)
from dsh_tutor.platform_vision import Perception
from dsh_tutor.platform_screen import Highlight
from test_platform_api import ASSESSMENT, EmptyCourses, answering, body, headers, settings, sse
from test_screen import capture
from test_vision import FakeVision, analyzer, perception

UI_QUESTION = "我找不到 Create Workflow 按钮在哪里"
ENV_QUESTION = "n8n 打不开，浏览器显示连接被拒绝，为什么？"
NOTE_UNUSED = "（这次没能查看你发来的虚拟机画面，以上回答没有参考画面。）"
NOTE_SENSITIVE = "（你发来的画面上可能有密钥或密码，我没有使用这张截图。请先关闭显示密钥的窗口，再重新发送画面。）"
NOTE_REQUEST = "如果按上面的步骤还是找不到，可以把现在的虚拟机画面发给我，我看看你停在哪一步。"


def sample_perception(**overrides):
    values = dict(readable=True, window="browser", app="Firefox", page="n8n - Overview",
                  summary="学生在 n8n 首页。", visible_errors=["Bad request"],
                  elements=[Highlight(label="Create Workflow", x=0.82, y=0.04, width=0.12, height=0.05)],
                  zoom=None, contains_sensitive=False)
    values.update(overrides)
    return Perception(**values)


class UnitTests(unittest.TestCase):
    def test_question_kinds(self):
        cases = {
            ENV_QUESTION: ENVIRONMENT,
            UI_QUESTION: UI,
            "找不到 Create Workflow": UI,
            "Execute workflow 点了没反应": UI,
            "docker compose up 报错了，怎么办？": ENVIRONMENT,
            "找不到 docker 命令": ENVIRONMENT,
            "Where is the Publish button?": UI,
            "permission denied when running docker": ENVIRONMENT,
            "Schedule Trigger 在哪里设置": UI,
            "Tavily 节点一直报 401 怎么办": OTHER,
            "什么是 n8n？": OTHER,
        }
        for question, kind in cases.items():
            self.assertEqual(classify_question(question), kind, question)

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
            values = dict(screen_attached=True, screen_used=False, screen_codes=[], kind=UI, requests_enabled=False,
                          language="zh-CN")
            values.update(overrides)
            return answer_note(**values)
        self.assertEqual(note(screen_used=True), "")
        self.assertEqual(note(screen_codes=[vision.SCREEN_SENSITIVE_CONTENT]), NOTE_SENSITIVE)
        self.assertIn("次数暂时用完", note(screen_codes=[vision.SCREEN_RATE_LIMITED]))
        for code in (screen.SCREEN_TOO_LARGE, screen.SCREEN_NOT_ANALYZED, vision.SCREEN_ANALYSIS_TIMEOUT):
            self.assertEqual(note(screen_codes=[code]), NOTE_UNUSED, code)
        self.assertEqual(note(screen_attached=False), "")                              # 开关关闭
        self.assertEqual(note(screen_attached=False, requests_enabled=True), NOTE_REQUEST)
        self.assertEqual(note(screen_attached=False, requests_enabled=True, kind=ENVIRONMENT), "")
        self.assertTrue(note(screen_attached=False, requests_enabled=True, language="en").startswith("If you"))


class FlowTests(unittest.TestCase):
    def run_flow(self, question, *, screen_attached=True, vision_script=(), assessment=None, requests=False,
                 runner=None):
        calls = []
        tool = analyzer(FakeVision(*vision_script), api_key_env=None)
        client = TestClient(create_app(
            settings=settings(model_provider="openai_compatible", model_api_key_set=True, total_timeout_seconds=40,
                              screen_requests=requests),
            runner=runner(calls) if runner else answering("在右上角。", calls=calls, assessment=assessment),
            course_repository=EmptyCourses(), screen_analyzer=tool))
        payload = body(question=question, **({"screen": capture().model_dump(mode="json")} if screen_attached else {}))
        frames = sse(client.post("/v1/agent/diagnose/stream", json=payload, headers=headers()).text)
        return frames, calls, frames[-2][1]["response"]

    def test_ui_question_with_screen(self):
        frames, calls, result = self.run_flow(UI_QUESTION)
        stage, system, prompt, tools, max_calls = calls[0]
        self.assertEqual((stage, max_calls), ("assessment", 2))  # 界面类问题评估最多 2 次
        self.assertIn("screen_observation", prompt); self.assertIn("右上", prompt)
        self.assertIn(SCREEN_RULES.strip()[:20], system)
        self.assertNotIn(capture().data[:60], prompt)  # 提示词里只有文字描述，没有图片
        answer_stage = calls[1]
        self.assertNotIn("screen_observation", answer_stage[2])  # 回答阶段只看校验过的评估（与 main 相同）
        self.assertEqual((result["status"], result["answer"]), ("completed", "在右上角。"))
        self.assertEqual(result["screen_findings"]["highlights"][0]["label"], "Create Workflow")
        self.assertNotIn(screen.SCREEN_REQUESTED, [e for e, _ in frames])

    def test_screen_reference_does_not_invalidate_the_assessment(self):
        cited = {**ASSESSMENT, "evidence_ids": ["screen-1"]}
        frames, calls, result = self.run_flow(UI_QUESTION, assessment=cited)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("MODEL_REFERENCED_UNKNOWN_EVIDENCE", result["limitations"])

    def test_environment_question_with_screen_keeps_the_normal_budget(self):
        frames, calls, result = self.run_flow(ENV_QUESTION)
        self.assertEqual(calls[0][4], 4)  # 默认的 max_reasoning_turns
        self.assertIn("screen_observation", calls[0][2])

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

    def test_without_screen_and_switch_off_is_identical_to_main(self):
        frames, calls, result = self.run_flow(UI_QUESTION, screen_attached=False)
        self.assertEqual(calls[0][4], 4)
        self.assertNotIn("screen_observation", calls[0][2])
        self.assertEqual(result["answer"], "在右上角。")
        self.assertFalse({e for e, _ in frames} & {screen.SCREEN_REQUESTED, screen.SCREEN_ANALYZED})

    def test_without_screen_and_switch_on(self):
        frames, calls, result = self.run_flow(UI_QUESTION, screen_attached=False, requests=True)
        names = [e for e, _ in frames]
        self.assertEqual(calls[0][4], 2)
        self.assertEqual(result["answer"], "在右上角。\n\n" + NOTE_REQUEST)
        self.assertEqual("".join(d["delta"] for e, d in frames if e == "assistant_delta"), result["answer"])
        self.assertLess(names.index("answer_finished"), names.index(screen.SCREEN_REQUESTED))
        self.assertEqual(next(d for e, d in frames if e == screen.SCREEN_REQUESTED)["reason"], "ui_question")
        env_frames, env_calls, env_result = self.run_flow(ENV_QUESTION, screen_attached=False, requests=True)
        self.assertEqual((env_calls[0][4], env_result["answer"]), (4, "在右上角。"))  # 环境类问题不提示
        self.assertNotIn(screen.SCREEN_REQUESTED, [e for e, _ in env_frames])

    def test_sensitive_screen(self):
        frames, calls, result = self.run_flow(UI_QUESTION, vision_script=[perception(contains_sensitive=True)])
        self.assertNotIn("screen_observation", calls[0][2])  # 不使用这张截图的内容
        self.assertEqual(calls[0][4], 2)  # 仍是界面类问题
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
