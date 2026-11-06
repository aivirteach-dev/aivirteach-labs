"""监控 span：结构、两套属性约定、token 换算、时间、失败标记、隐私默认值，以及监控出错不影响请求。

用 OpenTelemetry SDK 的内存导出器，不需要 Phoenix 或网络。
"""
import asyncio
import os
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from dsh_tutor import platform_models
from dsh_tutor.platform_app import Diagnoser, create_app
from dsh_tutor.platform_assessment import is_answer_stage
from dsh_tutor.platform_runtime import ModelCall, RunOutcome
from dsh_tutor.telemetry import RequestTrace, Telemetry, provider_name, tracing_requested
from test_platform_api import EmptyCourses, body, headers, settings

USAGE = {"inputTokens": 40, "outputTokens": 12, "totalTokens": 132, "cacheReadTokens": 80}


def memory_telemetry(capture_content=False):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return Telemetry(provider.get_tracer("test"), provider, provider_name="deepseek", model="deepseek-v4-flash",
                     capture_content=capture_content), exporter


ASSESSMENT = ('{"diagnosis": {"summary": "VM ok", "probable_causes": [], "confidence": "low"}, '
              '"course_alignment": {"expected": [], "observed": []}, "evidence_ids": [], '
              '"suggested_actions": [], "limitations": []}')


def tool_run(prompt, dsh, on_event, cancel, tools):
    """模拟“评估阶段调一次工具 → 回答阶段输出文字”，带事件文件里的毫秒时间戳。"""
    now = int(time.time() * 1000)
    if not is_answer_stage(dsh.system_prompt):
        return RunOutcome(ASSESSMENT, "completed", [USAGE], None,
                          calls=[ModelCall(1, now, None, now + 300, "toolUse", None, USAGE)],
                          tool_events=[{"name": "tool_started", "t": now + 310, "data": {"tool": "get_vm_status"}},
                                       {"name": "tool_finished", "t": now + 410, "data": {
                                           "tool": "get_vm_status", "status": "ok", "duration_ms": 100,
                                           "observation_id": "obs-001"}}])
    on_event("delta", {"call": 1, "text": "好"})
    return RunOutcome("好", "completed", [USAGE], None,
                      calls=[ModelCall(1, now + 120, now + 200, now + 600, "stop", None, USAGE)])


def spans_by_kind(exporter):
    return {kind: [s for s in exporter.get_finished_spans() if s.attributes["openinference.span.kind"] == kind]
            for kind in ("AGENT", "LLM", "TOOL")}


class SpanTests(unittest.TestCase):
    def run_app(self, runner, telemetry, **overrides):
        app = create_app(settings=settings(**overrides), runner=runner, course_repository=EmptyCourses(),
                         telemetry=telemetry)
        return TestClient(app).post("/v1/agent/diagnose", json=body(), headers=headers()).json()

    def test_one_trace_with_agent_llm_and_tool_spans(self):
        telemetry, exporter = memory_telemetry()
        data = self.run_app(tool_run, telemetry)
        spans = spans_by_kind(exporter)
        [root], llm, [tool] = spans["AGENT"], spans["LLM"], spans["TOOL"]
        self.assertEqual(len(llm), 2)
        self.assertEqual({s.context.trace_id for s in exporter.get_finished_spans()}, {root.context.trace_id})
        self.assertTrue(all(s.parent.span_id == root.context.span_id for s in [*llm, tool]))
        self.assertEqual(root.name, "invoke_agent aivirteach-tutor")
        self.assertEqual(root.attributes["aivirteach.request_id"], data["request_id"])
        self.assertEqual(root.attributes["aivirteach.status"], "completed")
        self.assertEqual((root.attributes["aivirteach.model_calls"], root.attributes["aivirteach.tool_calls"]), (2, 1))
        self.assertIn("aivirteach.queue_wait_ms", root.attributes)
        self.assertGreaterEqual(root.attributes["aivirteach.time_to_first_token_ms"], 200)  # 回答阶段的首字
        self.assertEqual(root.status.status_code, StatusCode.UNSET)
        self.assertEqual(tool.name, "execute_tool get_vm_status")
        self.assertEqual(tool.attributes["aivirteach.observation_id"], "obs-001")
        self.assertEqual((tool.end_time - tool.start_time) // 1_000_000, 100)

    def test_llm_span_carries_both_conventions_and_counts_cache_in_input(self):
        telemetry, exporter = memory_telemetry()
        self.run_app(tool_run, telemetry)
        second = sorted(spans_by_kind(exporter)["LLM"], key=lambda s: s.start_time)[1]
        a = second.attributes
        self.assertEqual(second.name, "chat deepseek-v4-flash")
        self.assertEqual((a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"],
                          a["gen_ai.usage.cache_read.input_tokens"]), (120, 12, 80))
        self.assertEqual((a["llm.token_count.prompt"], a["llm.token_count.completion"], a["llm.token_count.total"],
                          a["llm.token_count.prompt_details.cache_read"]), (120, 12, 132, 80))
        self.assertEqual((a["gen_ai.provider.name"], a["llm.provider"], a["llm.model_name"]),
                         ("deepseek", "deepseek", "deepseek-v4-flash"))
        self.assertEqual(tuple(a["gen_ai.response.finish_reasons"]), ("stop",))
        self.assertEqual([s.attributes["aivirteach.phase"] for s in sorted(spans_by_kind(exporter)["LLM"],
                                                                      key=lambda s: s.start_time)],
                         ["assessment", "answer"])
        self.assertEqual(a["aivirteach.time_to_first_token_ms"], 80)
        self.assertEqual((second.end_time - second.start_time) // 1_000_000, 480)

    def test_content_is_not_recorded_unless_enabled(self):
        telemetry, exporter = memory_telemetry()
        self.run_app(tool_run, telemetry)
        root = spans_by_kind(exporter)["AGENT"][0]
        self.assertNotIn("input.value", root.attributes)
        self.assertNotIn("output.value", root.attributes)
        telemetry, exporter = memory_telemetry(capture_content=True)
        self.run_app(tool_run, telemetry)
        root = spans_by_kind(exporter)["AGENT"][0]
        self.assertEqual(root.attributes["input.value"], body()["question"])
        self.assertEqual(root.attributes["output.value"], "好")

    def test_failures_are_marked_as_errors(self):
        def exhausted(prompt, dsh, on_event, cancel, tools):
            now = int(time.time() * 1000)
            return RunOutcome("", None, [], "MODEL_BUDGET_EXHAUSTED",
                              calls=[ModelCall(1, now, None, now + 5, None, "budget", None)],
                              tool_events=[{"name": "tool_finished", "t": now + 3, "data": {
                                  "tool": "get_guest_journal", "status": "denied", "duration_ms": 0,
                                  "error_code": "TOOL_NOT_ALLOWED"}}])
        telemetry, exporter = memory_telemetry()
        self.run_app(exhausted, telemetry)
        spans = spans_by_kind(exporter)
        root, llm, tool = spans["AGENT"][0], spans["LLM"][0], spans["TOOL"][0]
        self.assertEqual((root.status.status_code, root.attributes["error.type"]),
                         (StatusCode.ERROR, "MODEL_BUDGET_EXHAUSTED"))
        self.assertEqual((llm.status.status_code, llm.attributes["error.type"]), (StatusCode.ERROR, "budget"))
        self.assertEqual((tool.status.status_code, tool.attributes["error.type"]), (StatusCode.ERROR, "TOOL_NOT_ALLOWED"))

    def test_model_call_that_finishes_with_error_is_marked_as_failed(self):
        def unreachable(prompt, dsh, on_event, cancel, tools):
            now = int(time.time() * 1000)
            return RunOutcome("", "completed", [], None,
                              calls=[ModelCall(1, now, None, now + 5, "error", None, None)])
        telemetry, exporter = memory_telemetry()
        self.run_app(unreachable, telemetry)
        llm = spans_by_kind(exporter)["LLM"][0]
        self.assertEqual((llm.status.status_code, llm.attributes["error.type"]), (StatusCode.ERROR, "error"))

    def test_timeout_waits_for_dsh_to_finish_before_recording(self):
        telemetry, exporter = memory_telemetry()
        finished = threading.Event()

        def slow(prompt, dsh, on_event, cancel, tools):
            now = int(time.time() * 1000)
            cancel.wait(5)
            time.sleep(0.2)
            finished.set()
            return RunOutcome("", None, [], "CANCELLED", calls=[ModelCall(1, now, None, int(time.time() * 1000),
                                                                          None, "cancelled", None)])

        async def scenario():
            diagnoser = Diagnoser(settings(total_timeout_seconds=1), slow, EmptyCourses(), telemetry)
            response = await diagnoser.diagnose(platform_models.DiagnoseRequest.model_validate(body()))
            spans_at_response = len(exporter.get_finished_spans())
            await asyncio.sleep(0.6)
            return response, spans_at_response
        response, spans_at_response = asyncio.run(scenario())
        self.assertIn("AGENT_TOTAL_TIMEOUT", response.limitations)
        self.assertTrue(finished.is_set())
        self.assertEqual(spans_at_response, 0)
        spans = spans_by_kind(exporter)
        self.assertEqual(spans["AGENT"][0].attributes["error.type"], "AGENT_TOTAL_TIMEOUT")
        self.assertEqual(spans["LLM"][0].attributes["error.type"], "cancelled")

    def test_telemetry_errors_never_break_the_request(self):
        class Broken:
            def start_span(self, *args, **kwargs):
                raise RuntimeError("collector exploded")
        telemetry = Telemetry(Broken(), provider_name="deepseek", model="m")
        with self.assertLogs("aivirteach.dsh_agent.telemetry", "WARNING"):
            data = self.run_app(tool_run, telemetry)
        self.assertEqual(data["status"], "completed")


class ConfigTests(unittest.TestCase):
    def test_disabled_without_an_otlp_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(tracing_requested())
            self.assertFalse(Telemetry.from_env(provider_name="x", model="m").enabled)
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
                                          "OTEL_SDK_DISABLED": "true"}, clear=True):
            self.assertFalse(tracing_requested())
        Telemetry().record(None, None, RequestTrace(), stream=False)  # 关闭时是空操作

    def test_resource_defaults_can_be_overridden_by_standard_env(self):
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}, clear=True):
            telemetry = Telemetry.from_env(provider_name="deepseek", model="m")
            resource = telemetry._provider.resource.attributes
            self.assertEqual((resource["service.name"], resource["openinference.project.name"]),
                             ("aivirteach-dsh-agent", "aivirteach-agent"))
            telemetry.shutdown()
        with mock.patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
                                          "OTEL_RESOURCE_ATTRIBUTES": "openinference.project.name=staging"},
                             clear=True):
            telemetry = Telemetry.from_env(provider_name="deepseek", model="m")
            self.assertEqual(telemetry._provider.resource.attributes["openinference.project.name"], "staging")
            telemetry.shutdown()

    def test_provider_name(self):
        self.assertEqual(provider_name("openai_compatible", "https://api.deepseek.com/v1"), "deepseek")
        self.assertEqual(provider_name("openai_compatible", "https://notdeepseek.com/v1"), "openai_compatible")
        self.assertEqual(provider_name("ollama", "http://h:11434/v1"), "ollama")


if __name__ == "__main__":
    unittest.main()
