"""按课程确定工具范围、预先检查，以及它们接进诊断流程的方式。只用假 Gateway 和假模型。"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from dsh_tutor import platform_precheck as precheck
from dsh_tutor.platform_app import create_app
from dsh_tutor.platform_assessment import PRECHECK_RULES
from dsh_tutor.platform_mcp import ToolBridge, ToolContext
from dsh_tutor.platform_models import DiagnoseRequest, DiagnosticScope, ToolName
from dsh_tutor.platform_tools import validate_tool_call
from sandbox import fake_gateway, fake_model
from test_platform_api import GATEWAY_TOKEN, EmptyCourses, answering, body, headers, settings, sse

COURSE = {"course_id": "ai-daily-briefing", "version": 1, "title": "AI Daily Briefing"}
STEP = {"module_id": "m1", "lesson_id": "install-and-start-n8n", "sequence": 2, "title": "Install and Start n8n"}
SERVER_SCOPE = {"allowed_tools": ["get_vm_status", "check_guest_port"], "allowed_ports": [5678]}


def request(**overrides):
    return DiagnoseRequest.model_validate(body(**{"course": COURSE, "current_step": STEP, **overrides}))


def context(gateway_url, lab_id="lab-001", scope=None, **overrides):
    values = dict(lab_id=lab_id, scope=scope or precheck.load_config().courses[0].scope, gateway_url=gateway_url,
                  gateway_token=GATEWAY_TOKEN, events_path="", deadline_unix=time.time() + 30)
    values.update(overrides)
    return ToolContext(**values)


class ResolveTests(unittest.TestCase):
    config = precheck.load_config()

    def test_bundled_config_is_valid_and_every_precheck_is_inside_its_scope(self):
        self.assertTrue(self.config.courses)
        for course in self.config.courses:
            for check in [*course.prechecks, *(c for lesson in course.lessons for c in lesson.prechecks)]:
                validate_tool_call(check.tool.value, check.arguments, course.scope)  # 不合规会抛 ToolPolicyError
        default = DiagnosticScope()
        for check in self.config.default_prechecks:
            validate_tool_call(check.tool.value, check.arguments, default)

    def test_course_scope_is_used_when_the_server_sends_none(self):
        resolved, checks = precheck.resolve(request(), self.config)
        self.assertEqual(resolved.diagnostic_scope.allowed_containers, ["ai-daily-briefing-n8n"])
        self.assertIn(ToolName.GET_GUEST_CONTAINER_LOGS, resolved.diagnostic_scope.allowed_tools)
        self.assertEqual([c.tool.value for c in checks], ["get_vm_status", "get_guest_container_status", "check_guest_port"])
        # 教材里的正式编号和 Server 用的别名都能匹配
        for course_id in ("n8n-agent-builder", "AI-Daily-Briefing"):
            resolved, _ = precheck.resolve(request(course=dict(COURSE, course_id=course_id)), self.config)
            self.assertEqual(resolved.diagnostic_scope.allowed_ports, [5678], course_id)

    def test_lesson_specific_checks(self):
        _, checks = precheck.resolve(request(current_step=dict(STEP, lesson_id="install-docker")), self.config)
        self.assertEqual([(c.tool.value, c.arguments) for c in checks],
                         [("get_vm_status", {}), ("get_guest_service_status", {"service": "docker.service"})])

    def test_server_scope_wins_and_limits_the_prechecks(self):
        resolved, checks = precheck.resolve(request(diagnostic_scope=SERVER_SCOPE), self.config)
        self.assertEqual(resolved.diagnostic_scope.allowed_tools, {ToolName.GET_VM_STATUS, ToolName.CHECK_GUEST_PORT})
        self.assertEqual([c.tool.value for c in checks], ["get_vm_status", "check_guest_port"])  # 容器检查不在范围内
        resolved, checks = precheck.resolve(request(diagnostic_scope={"allowed_tools": []}), self.config)
        self.assertEqual((resolved.diagnostic_scope.allowed_tools, checks), (set(), []))  # 明确不给工具：和 main 一样

    def test_unknown_course_keeps_mains_default_scope(self):
        resolved, checks = precheck.resolve(request(course=dict(COURSE, course_id="other")), self.config)
        self.assertEqual(resolved.diagnostic_scope, DiagnosticScope())
        self.assertEqual([c.tool.value for c in checks], ["get_vm_status", "get_guest_agent_status"])

    def test_missing_or_invalid_file_falls_back_to_no_config(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "scopes.json"
            broken.write_text('{"courses": [{"course_ids": []}]}', encoding="utf-8")
            with self.assertLogs("aivirteach.dsh_agent", "ERROR"):
                self.assertEqual(precheck.load_config(broken), precheck.ScopeConfig())
            with self.assertLogs("aivirteach.dsh_agent", "ERROR"):
                self.assertEqual(precheck.load_config(Path(directory) / "missing.json"), precheck.ScopeConfig())


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["AIVIRTEACH_DIAGNOSTIC_TOKEN"] = GATEWAY_TOKEN

    def run_checks(self, lab_id="lab-001"):
        _, checks = precheck.resolve(request(), precheck.load_config())
        events = []
        with fake_model.serve(fake_gateway.app) as url:
            done = precheck.run(checks, context(url, lab_id), lambda kind, data: events.append((kind, data)))
        return done, events

    def test_checks_run_in_parallel_and_look_like_tool_calls(self):
        done, events = self.run_checks()
        self.assertEqual([item["tool"] for item in done.items],
                         ["get_vm_status", "get_guest_container_status", "check_guest_port"])
        self.assertEqual(sorted(item["observation_id"] for item in done.items), ["obs-001", "obs-002", "obs-003"])
        self.assertEqual(sorted(e["id"] for e in done.evidence), ["obs-001", "obs-002", "obs-003"])
        self.assertEqual({t["status"] for t in done.traces}, {"ok"})
        self.assertEqual([kind for kind, _ in events], ["tool_event"] * 6)
        self.assertEqual(sorted(e["name"] for _, e in events), ["tool_finished"] * 3 + ["tool_started"] * 3)
        self.assertEqual(done.tool_events, [data for _, data in events])
        port = next(item for item in done.items if item["tool"] == "check_guest_port")
        self.assertIn('\\"reachable\\": false', port["data"])  # 数据和工具返回的相同（已脱敏、截短）
        self.assertEqual((done.prefetch.evidence_count, len(done.prefetch.cache)), (3, 3))

    def test_gateway_errors_are_recorded_like_tool_errors(self):
        done, _ = self.run_checks("lab-unknown")
        self.assertEqual({item["error_code"] for item in done.items}, {"VM_NOT_FOUND"})
        self.assertEqual(done.evidence, [])
        self.assertIn("VM_NOT_FOUND", done.limitations)
        self.assertTrue(done.partial)

    def test_large_data_is_cut_for_the_prompt(self):
        original = precheck.MAX_DATA_CHARS
        precheck.MAX_DATA_CHARS = 40
        try:
            done, _ = self.run_checks()
        finally:
            precheck.MAX_DATA_CHARS = original
        for item in done.items:
            self.assertLessEqual(len(item["data"]), 40)
            self.assertTrue(item["data_truncated"])

    def test_tool_bridge_continues_after_the_prechecks(self):
        done, _ = self.run_checks()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prefetch.json"
            path.write_text(done.prefetch.model_dump_json(), encoding="utf-8")
            rows = []
            with fake_model.serve(fake_gateway.app) as url:
                fake_gateway.app.state.calls.clear()
                bridge = ToolBridge(context(url, prefetch_path=str(path), max_tool_calls=2), sink=rows.append)

                async def calls():
                    try:
                        repeated = await bridge.call("check_guest_port", {"port": 5678})
                        logs = await bridge.call("get_guest_container_logs", {"container": "ai-daily-briefing-n8n"})
                        third = await bridge.call("get_guest_agent_status", {})
                        return repeated, logs, third
                    finally:
                        await bridge.client.aclose()
                repeated, logs, third = asyncio.run(calls())
            self.assertTrue(repeated["ok"])  # 和预先检查相同：直接用已有结果，不再访问 Gateway
            self.assertEqual([tool for _, tool in fake_gateway.app.state.calls], ["get_guest_container_logs"])
            self.assertEqual(logs["observation_id"], "obs-004")  # 编号接着往下排
            self.assertEqual(third["error_code"], "TOOL_CALL_BUDGET_EXHAUSTED")  # 模型自己的次数（2）用完；预先检查不占
            statuses = [row["trace"]["status"] for row in rows if "trace" in row]
            self.assertEqual(statuses, ["cached", "ok", "denied"])


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["AIVIRTEACH_DIAGNOSTIC_TOKEN"] = GATEWAY_TOKEN

    def stream(self, payload, calls, scopes=None):
        with fake_model.serve(fake_gateway.app) as url:
            app = create_app(settings=settings(gateway_url=url, total_timeout_seconds=30), course_repository=EmptyCourses(),
                             runner=answering("好", calls=calls), scopes=scopes or precheck.load_config())
            text = TestClient(app).post("/v1/agent/diagnose/stream", json=payload, headers=headers()).text
        return sse(text)

    def test_prechecks_feed_the_assessment_with_main_event_order(self):
        calls = []
        frames = self.stream(body(course=COURSE, current_step=STEP), calls)
        names = [name for name, _ in frames]
        start, end = names.index("reasoning_started"), names.index("reasoning_finished")
        self.assertEqual(sorted(names[start + 1:end]), ["tool_finished"] * 3 + ["tool_started"] * 3)  # 和 main 一样在这两个之间
        self.assertEqual(frames[end][1]["tool_call_count"], 3)
        stage, system, prompt, tools, _ = calls[0]
        self.assertIn(PRECHECK_RULES, system)
        block = json.loads(prompt.rsplit("\n\n", 1)[1])
        self.assertEqual(len(block["prechecked"]), 3)
        self.assertEqual(tools.scope.allowed_containers, ["ai-daily-briefing-n8n"])  # 模型还能调课程范围内的工具
        result = frames[-2][1]["response"]
        self.assertEqual(len(result["tool_trace"]), 3)
        self.assertNotIn("LIVE_DIAGNOSTICS_NOT_RUN", result["limitations"])
        self.assertEqual(calls[1][0], "answer")

    def test_system_prompt_does_not_depend_on_the_prechecks(self):
        with_checks, without = [], []
        self.stream(body(course=COURSE, current_step=STEP), with_checks)
        self.stream(body(course=COURSE, current_step=STEP), without,
                    scopes=precheck.ScopeConfig(courses=[precheck.load_config().courses[0].model_copy(
                        update={"prechecks": [], "lessons": []})]))
        self.assertEqual(with_checks[0][1], without[0][1])  # DSH 可以先启动，系统消息也总能命中缓存
        self.assertNotIn("prechecked", without[0][2])

    def test_no_tools_means_no_prechecks(self):
        calls = []
        frames = self.stream(body(course=COURSE, current_step=STEP, diagnostic_scope={"allowed_tools": []}), calls)
        self.assertNotIn("tool_started", [name for name, _ in frames])
        self.assertIsNone(calls[0][3])
        self.assertIn("LIVE_DIAGNOSTICS_NOT_RUN", frames[-2][1]["response"]["limitations"])


if __name__ == "__main__":
    unittest.main()
