"""平台接口契约：以 main 上 Server/Client 已在用的格式为准，不调用真实模型或真实 Gateway。"""
import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from dsh_tutor import platform_models
from dsh_tutor.platform_app import (Diagnoser, Settings, _AnswerFilter, create_app, normalize_base_url,
                                    normalize_usage)
from dsh_tutor.platform_mcp import ToolBridge, ToolContext
from dsh_tutor.platform_runtime import RunOutcome

ROOT = Path(__file__).resolve().parents[2]
MAIN_AGENT = ROOT.parent / "aivirteach_agent"
TOKEN = "t" * 40
GATEWAY_TOKEN = "g" * 40
# Client 的 read-tutor-stream.ts 认识的事件，加上 Server 自己处理的 result / error。
CLIENT_EVENTS = {"accepted", "started", "reasoning_started", "reasoning_finished", "context_ready", "tool_started",
                 "tool_finished", "finalization_started", "assessment_validating", "assessment_validated",
                 "answer_started", "answer_failed", "answer_finished", "assistant_delta", "result", "done", "error"}


def settings(**overrides):
    values = dict(agent_token=TOKEN, model_provider="ollama", model_base_url="http://127.0.0.1:1/v1",
                  model_name="test-model", model_api_key_set=False, model_thinking="disabled",
                  total_timeout_seconds=5, max_concurrent_requests=2, context_window=32768,
                  max_output_tokens=1024, state_dir=Path("/nonexistent"), diagnostic_token=GATEWAY_TOKEN)
    values.update(overrides)
    return Settings(**values)


def body(**overrides):
    value = {"request_id": "a10beac8-d1db-4b1a-8df0-79aa8208e273", "lab_id": "lab-001", "question": "n8n 打不开？",
             "course": {"course_id": "n8n", "version": 1, "title": "n8n course"},
             "current_step": {"module_id": "m1", "lesson_id": "install-n8n", "sequence": 4, "title": "Install n8n"}}
    value.update(overrides)
    return value


def answering(*pieces, finish="completed"):
    def runner(prompt, dsh, on_event, cancel, tools):
        for piece in pieces:
            on_event("delta", {"call": 1, "text": piece})
        on_event("finish", {"call": 1, "kind": "stop"})
        return RunOutcome("".join(pieces), finish, [])
    return runner


def failing(prompt, dsh, on_event, cancel, tools):
    raise RuntimeError("upstream down")


def sleeping(prompt, dsh, on_event, cancel, tools):
    time.sleep(1.5)
    return RunOutcome("late", "completed", [])


class EmptyCourses:
    course_count = 0

    def enrich(self, request):
        return request


def client(runner=None, **overrides):
    return TestClient(create_app(settings=settings(**overrides), runner=runner or answering("你好", "，世界"),
                                 course_repository=EmptyCourses()))


def headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def sse(text):
    frames, event = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            frames.append((event, json.loads(line[5:])))
    return frames


def assert_server_accepts(test, response):
    """对应 Server agent-client.ts 的 DiagnoseResponseSchema。"""
    test.assertIn(response["status"], ("completed", "partial"))
    test.assertTrue(isinstance(response["answer"], str) and response["answer"])
    for key in ("evidence", "suggested_actions", "limitations", "tool_trace"):
        test.assertIsInstance(response[key], list)
    test.assertIn("diagnosis", response); test.assertIn("course_alignment", response)
    usage = response.get("usage")
    if usage is not None:
        test.assertEqual(set(usage), {"input_cache_hit_tokens", "input_cache_miss_tokens", "output_tokens"})


def load_main(name):
    """按包名加载 main 的模块；main 面向 Python 3.11+，在 3.10 上导入失败时跳过对比。"""
    package = "main_agent"
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(package, MAIN_AGENT / "__init__.py",
                                                      submodule_search_locations=[str(MAIN_AGENT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{package}.{name}")


class ContractCopyTests(unittest.TestCase):
    def main_or_skip(self, name):
        try:
            return load_main(name)
        except ImportError as exc:
            self.skipTest(f"main agent not importable on this Python: {exc}")

    def test_models_match_main_agent(self):
        main = self.main_or_skip("models")
        for name in ("DiagnoseRequest", "DiagnoseResponse"):
            self.assertEqual(getattr(platform_models, name).model_json_schema(),
                             getattr(main, name).model_json_schema(), name)

    def test_tools_match_main_agent(self):
        main = self.main_or_skip("tools")
        from dsh_tutor import platform_tools
        self.assertEqual({t.value for t in platform_tools.TOOL_SPECS}, {t.value for t in main.TOOL_SPECS})
        for tool, spec in platform_tools.TOOL_SPECS.items():
            other = main.TOOL_SPECS[main.ToolName(tool.value)]
            self.assertEqual(spec.description, other.description, tool)
            self.assertEqual(spec.input_schema(), other.args_model.model_json_schema(), tool)


class AuthAndValidationTests(unittest.TestCase):
    def test_health_is_public(self):
        self.assertEqual(client().get("/health").json(), {"status": "ok"})

    def test_missing_or_wrong_token_is_401(self):
        c = client()
        self.assertEqual(c.post("/v1/agent/diagnose", json=body()).status_code, 401)
        self.assertEqual(c.post("/v1/agent/diagnose", json=body(), headers=headers("x" * 40)).status_code, 401)
        self.assertEqual(c.post("/v1/agent/diagnose/stream", json=body(), headers=headers("x" * 40)).status_code, 401)

    def test_unconfigured_token_is_503(self):
        self.assertEqual(client(agent_token="").post("/v1/agent/diagnose", json=body(), headers=headers()).status_code, 503)

    def test_not_ready_is_503(self):
        for broken in ({"model_name": ""}, {"diagnostic_token": ""}):
            c = client(**broken)
            self.assertEqual(c.get("/ready").status_code, 503, broken)
            self.assertEqual(c.post("/v1/agent/diagnose", json=body(), headers=headers()).status_code, 503, broken)

    def test_question_limit_matches_server(self):
        c = client()
        self.assertEqual(c.post("/v1/agent/diagnose", json=body(question="问" * 4000), headers=headers()).status_code, 200)
        self.assertEqual(c.post("/v1/agent/diagnose", json=body(question="问" * 4001), headers=headers()).status_code, 422)

    def test_unknown_fields_and_bad_ids_are_422(self):
        c = client()
        self.assertEqual(c.post("/v1/agent/diagnose", json=body(extra=1), headers=headers()).status_code, 422)
        self.assertEqual(c.post("/v1/agent/diagnose", json=body(request_id="nope"), headers=headers()).status_code, 422)

    def test_history_scope_and_learner_state_are_accepted(self):
        extra = {"history": [{"role": "user", "content": "之前的问题"}], "learner_state": {"currentLessonId": "x"},
                 "diagnostic_scope": {"allowed_tools": ["get_vm_status"]}}
        self.assertEqual(client().post("/v1/agent/diagnose", json=body(**extra), headers=headers()).status_code, 200)


class ResponseTests(unittest.TestCase):
    def test_json_response_is_accepted_by_server_schema(self):
        data = client().post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        assert_server_accepts(self, data)
        self.assertEqual((data["status"], data["answer"]), ("completed", "你好，世界"))

    def test_failure_returns_partial_fallback_not_5xx(self):
        data = client(failing).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        assert_server_accepts(self, data)
        self.assertEqual(data["status"], "partial"); self.assertIn("MODEL_PROVIDER_ERROR", data["limitations"])

    def test_incomplete_run_is_partial(self):
        data = client(answering("半句", finish="error")).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual(data["status"], "partial"); self.assertIn("MODEL_STREAM_INCOMPLETE", data["limitations"])

    def test_total_timeout_is_partial(self):
        data = client(sleeping, total_timeout_seconds=1).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual(data["status"], "partial"); self.assertIn("AGENT_TOTAL_TIMEOUT", data["limitations"])


class StreamTests(unittest.TestCase):
    def stream(self, runner=None, payload=None, **overrides):
        response = client(runner, **overrides).post("/v1/agent/diagnose/stream", json=payload or body(), headers=headers())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return sse(response.text)

    def test_frames_match_main_format_and_order(self):
        frames = self.stream()
        names = [e for e, _ in frames]
        self.assertEqual(names[0], "accepted"); self.assertEqual(names[-2:], ["result", "done"])
        self.assertLess(names.index("answer_started"), names.index("assistant_delta"))
        self.assertLess(names.index("assistant_delta"), names.index("answer_finished"))
        self.assertTrue(set(names) <= CLIENT_EVENTS, set(names) - CLIENT_EVENTS)
        self.assertEqual([d["sequence"] for _, d in frames], list(range(1, len(frames) + 1)))
        self.assertTrue(all(d["schema_version"] == 1 and d["request_id"] == body()["request_id"] for _, d in frames))
        result = frames[-2][1]["response"]
        assert_server_accepts(self, result)
        self.assertEqual("".join(d["delta"] for e, d in frames if e == "assistant_delta"), result["answer"])

    def test_failure_still_ends_with_result_and_done(self):
        names = [e for e, _ in self.stream(failing)]
        self.assertIn("answer_failed", names); self.assertEqual(names[-2:], ["result", "done"])

    def test_tool_run_streams_tool_events_evidence_and_hides_preamble(self):
        evidence = {"id": "obs-001", "tool": "get_vm_status", "summary": "VM is running.",
                    "captured_at": "2026-11-05T00:00:00Z"}
        trace = {"tool": "get_vm_status", "status": "ok", "duration_ms": 12, "observation_id": "obs-001"}

        def with_tools(prompt, dsh, on_event, cancel, tools):
            assert tools is not None and tools.lab_id == "lab-001" and tools.gateway_token == GATEWAY_TOKEN
            on_event("delta", {"call": 1, "text": "我先查一下。"})
            on_event("block", {"call": 1, "type": "toolCall"})
            on_event("tool_event", {"name": "tool_started", "data": {"tool": "get_vm_status"}})
            on_event("tool_event", {"name": "tool_finished", "data": {"tool": "get_vm_status", "status": "ok"}})
            on_event("finish", {"call": 1, "kind": "tool-calls"})
            on_event("delta", {"call": 2, "text": "虚拟机正在运行。"})
            on_event("finish", {"call": 2, "kind": "stop"})
            return RunOutcome("虚拟机正在运行。", "completed", [], None, [evidence], [trace], [], False)

        frames = self.stream(with_tools)
        names = [e for e, _ in frames]
        self.assertLess(names.index("tool_finished"), names.index("answer_started"))
        self.assertEqual("".join(d["delta"] for e, d in frames if e == "assistant_delta"), "虚拟机正在运行。")
        result = frames[-2][1]["response"]
        assert_server_accepts(self, result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([e["id"] for e in result["evidence"]], ["obs-001"])
        self.assertEqual(result["tool_trace"][0]["status"], "ok")
        self.assertNotIn("LIVE_DIAGNOSTICS_NOT_RUN", result["limitations"])

    def test_denied_tool_makes_result_partial(self):
        trace = {"tool": "get_guest_journal", "status": "denied", "duration_ms": 1, "error_code": "TOOL_NOT_ALLOWED"}
        runner = lambda p, d, e, c, t: RunOutcome("好", "completed", [], None, [], [trace], [], True)
        result = self.stream(runner)[-2][1]["response"]
        self.assertEqual(result["status"], "partial")

    def test_empty_scope_runs_without_tools(self):
        seen = {}

        def record(prompt, dsh, on_event, cancel, tools):
            seen["tools"], seen["prompt"] = tools, prompt
            return answering("好")(prompt, dsh, on_event, cancel, tools)
        result = self.stream(record, body(diagnostic_scope={"allowed_tools": []}))[-2][1]["response"]
        self.assertIsNone(seen["tools"]); self.assertIn("本轮没有可用的检查工具", seen["prompt"])
        self.assertIn("LIVE_DIAGNOSTICS_NOT_RUN", result["limitations"])


class AnswerFilterTests(unittest.TestCase):
    def test_short_text_before_tool_call_is_dropped(self):
        f = _AnswerFilter()
        self.assertEqual(f.delta(1, "我先查一下"), [])
        self.assertEqual(f.block(1, "toolCall"), [])
        self.assertEqual(f.finish(1, "tool-calls"), [])
        self.assertEqual(f.delta(2, "结论"), [])
        self.assertEqual(f.finish(2, "stop"), ["结论"])

    def test_long_answer_starts_streaming_without_waiting_for_finish(self):
        f = _AnswerFilter()
        self.assertEqual(f.delta(1, "字" * 119), [])
        self.assertEqual(f.delta(1, "字"), ["字" * 119, "字"])
        self.assertEqual(f.delta(1, "后续"), ["后续"])


class ToolBridgeTests(unittest.TestCase):
    """工具桥对照 main orchestrator 的行为：校验、次数上限、缓存、Gateway 错误码、证据编号。"""

    def bridge(self, handler, scope=None, **overrides):
        events = Path(tempfile.mkdtemp()) / "events.jsonl"
        context = ToolContext(lab_id="lab-001", scope=platform_models.DiagnosticScope(**(scope or {})),
                              gateway_url="http://gateway.test:8765", gateway_token=GATEWAY_TOKEN,
                              events_path=str(events), deadline_unix=time.time() + 30, **overrides)
        calls = []

        def transport(request):
            calls.append(request)
            return handler(request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        return ToolBridge(context, client), calls, events

    @staticmethod
    def ok(request):
        return httpx.Response(200, json={"ok": True, "summary": "VM is running.", "data": {"state": "running"}})

    @staticmethod
    def rows(events):
        return [json.loads(line) for line in events.read_text().splitlines()]

    def test_request_matches_gateway_contract(self):
        bridge, calls, events = self.bridge(self.ok)
        payload = asyncio.run(bridge.call("get_vm_status", {}))
        request = calls[0]
        self.assertEqual(str(request.url), "http://gateway.test:8765/v1/diagnostics/lab-001/tools/get_vm_status")
        self.assertEqual(request.headers["authorization"], f"Bearer {GATEWAY_TOKEN}")
        self.assertEqual(json.loads(request.content), {"parameters": {}})
        self.assertEqual((payload["ok"], payload["observation_id"]), (True, "obs-001"))
        kinds = [next(iter(r)) for r in self.rows(events)]
        self.assertEqual(kinds, ["tool_event", "evidence", "trace", "tool_event"])

    def test_out_of_scope_calls_are_denied_before_gateway(self):
        bridge, calls, events = self.bridge(self.ok)
        payload = asyncio.run(bridge.call("get_guest_journal", {"service": "docker.service"}))
        self.assertEqual(payload["error_code"], "TOOL_NOT_ALLOWED"); self.assertEqual(calls, [])
        self.assertIn({"partial": True}, self.rows(events))
        scope = {"allowed_tools": ["get_guest_service_status"], "allowed_services": ["docker.service"]}
        bridge, calls, _ = self.bridge(self.ok, scope)
        self.assertEqual(asyncio.run(bridge.call("get_guest_service_status", {"service": "ssh.service"}))["error_code"],
                         "SERVICE_NOT_ALLOWED")
        self.assertEqual(calls, [])

    def test_call_budget_counts_denied_calls(self):
        bridge, calls, events = self.bridge(self.ok, max_tool_calls=2)
        asyncio.run(bridge.call("nope", {})); asyncio.run(bridge.call("get_vm_status", {}))
        payload = asyncio.run(bridge.call("get_vm_status", {}))
        self.assertEqual(payload["error_code"], "TOOL_CALL_BUDGET_EXHAUSTED"); self.assertEqual(len(calls), 1)
        self.assertIn({"limitation": "TOOL_CALL_BUDGET_EXHAUSTED"}, self.rows(events))

    def test_identical_calls_are_cached(self):
        bridge, calls, events = self.bridge(self.ok)
        first = asyncio.run(bridge.call("get_vm_status", {}))
        second = asyncio.run(bridge.call("get_vm_status", {}))
        self.assertEqual(first, second); self.assertEqual(len(calls), 1)
        self.assertEqual([r["trace"]["status"] for r in self.rows(events) if "trace" in r], ["ok", "cached"])

    def test_gateway_status_codes_match_main(self):
        expected = {401: "GATEWAY_AUTH_FAILED", 403: "GATEWAY_DENIED", 404: "VM_NOT_FOUND",
                    409: "GUEST_AGENT_UNAVAILABLE", 422: "GATEWAY_INVALID_ARGUMENT", 504: "GATEWAY_TIMEOUT",
                    500: "GATEWAY_ERROR"}
        for status_code, code in expected.items():
            bridge, _, events = self.bridge(lambda r, s=status_code: httpx.Response(s, json={"detail": "x"}))
            self.assertEqual(asyncio.run(bridge.call("get_vm_status", {}))["error_code"], code)
            self.assertIn({"limitation": code}, self.rows(events))

    def test_unreachable_gateway_is_reported(self):
        def down(request):
            raise httpx.ConnectError("refused")
        bridge, _, _ = self.bridge(down)
        self.assertEqual(asyncio.run(bridge.call("get_vm_status", {}))["error_code"], "GATEWAY_UNAVAILABLE")

    def test_secrets_in_tool_output_are_redacted(self):
        leak = lambda r: httpx.Response(200, json={"summary": "env", "data": "password=hunter2 token=abc"})
        bridge, _, _ = self.bridge(leak)
        payload = asyncio.run(bridge.call("get_vm_status", {}))
        self.assertNotIn("hunter2", json.dumps(payload)); self.assertGreater(payload["redaction_count"], 0)

    def test_token_is_not_in_repr(self):
        bridge, _, _ = self.bridge(self.ok)
        self.assertNotIn(GATEWAY_TOKEN, repr(bridge.ctx))


class UsageTests(unittest.TestCase):
    # 两组数据均为 DeepSeek 经 DSH 的实测值（第二次命中缓存）。
    FIRST = {"inputTokens": 1867, "outputTokens": 25, "totalTokens": 1892}
    CACHED = {"inputTokens": 202, "outputTokens": 25, "totalTokens": 1891, "cacheReadTokens": 1664}

    def test_deepseek_split_maps_to_main_fields(self):
        self.assertEqual(normalize_usage([self.CACHED]).model_dump(),
                         {"input_cache_hit_tokens": 1664, "input_cache_miss_tokens": 202, "output_tokens": 25})
        self.assertEqual(normalize_usage([self.FIRST, self.CACHED]).model_dump(),
                         {"input_cache_hit_tokens": 1664, "input_cache_miss_tokens": 2069, "output_tokens": 50})

    def test_inconsistent_or_missing_usage_is_null_not_zero(self):
        self.assertIsNone(normalize_usage([]))
        self.assertIsNone(normalize_usage([{**self.FIRST, "totalTokens": 9999}]))
        self.assertIsNone(normalize_usage([{"outputTokens": 3}]))
        self.assertIsNone(normalize_usage([{**self.FIRST, "cacheWriteTokens": 5}]))

    def test_ollama_usage_is_not_metered(self):
        self.assertIsNone(normalize_usage([{"inputTokens": 71, "outputTokens": 71, "totalTokens": 142}], "ollama"))


class DeploymentConfigTests(unittest.TestCase):
    """main 的 agent.env 原样可用：根地址、THINKING 为空或 disabled、fake 提供方。"""

    def test_root_base_url_gets_v1(self):
        self.assertEqual(normalize_base_url("https://api.deepseek.com/"), "https://api.deepseek.com/v1")
        self.assertEqual(normalize_base_url("http://h:11434/v1/"), "http://h:11434/v1")
        self.assertEqual(normalize_base_url(""), "")

    def test_main_agent_env_values_are_ready(self):
        for thinking in ("", "disabled", "enabled"):
            ready = settings(model_provider="openai_compatible", model_base_url="https://api.deepseek.com/v1",
                             model_name="deepseek-v4-flash", model_api_key_set=True, model_thinking=thinking)
            self.assertEqual(ready.readiness_errors(), [], thinking)
        self.assertTrue(settings(model_base_url="https://x/api").readiness_errors())

    def test_thinking_is_off_unless_enabled(self):
        self.assertTrue(settings(model_thinking="").dsh(10).reasoning_off)
        self.assertFalse(settings(model_thinking="enabled").dsh(10).reasoning_off)

    def test_reasoning_turns_map_to_model_calls(self):
        self.assertEqual(settings(max_reasoning_turns=4).dsh(10).max_model_calls, 5)

    def test_tokens_are_not_in_settings_repr(self):
        self.assertNotIn(GATEWAY_TOKEN, repr(settings())); self.assertNotIn(TOKEN, repr(settings()))

    def test_fake_provider_needs_no_model_and_streams(self):
        app = create_app(settings=settings(model_provider="fake", model_base_url="", model_name=""),
                         course_repository=EmptyCourses())
        c = TestClient(app)
        self.assertEqual(c.get("/ready").json()["provider"], "fake")
        frames = sse(c.post("/v1/agent/diagnose/stream", json=body(), headers=headers()).text)
        result = frames[-2][1]["response"]
        self.assertEqual(result["status"], "completed"); self.assertIn("fake", result["answer"])

    def test_course_repository_enriches_request(self):
        seen = {}

        class Courses(EmptyCourses):
            course_count = 3

            def enrich(self, request):
                seen["lesson"] = request.current_step.lesson_id
                return request
        c = TestClient(create_app(settings=settings(), runner=answering("好"), course_repository=Courses()))
        self.assertEqual(c.get("/ready").json()["processed_courses"], 3)
        c.post("/v1/agent/diagnose", json=body(), headers=headers())
        self.assertEqual(seen, {"lesson": "install-n8n"})


class BudgetAndCancelTests(unittest.TestCase):
    def test_truncated_answer_is_kept_as_partial(self):
        def truncated(prompt, dsh, on_event, cancel, tools):
            on_event("delta", {"call": 1, "text": "前半段"})
            return RunOutcome("前半段", "completed", [], "MODEL_STREAM_OUTPUT_TRUNCATED")
        data = client(truncated).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual((data["status"], data["answer"]), ("partial", "前半段"))
        self.assertIn("MODEL_STREAM_OUTPUT_TRUNCATED", data["limitations"])

    def test_run_budget_never_exceeds_total_timeout(self):
        seen = {}

        def record(prompt, dsh, on_event, cancel, tools):
            seen["timeout"] = dsh.total_timeout_seconds
            return RunOutcome("好", "completed", [])
        client(record, total_timeout_seconds=7).post("/v1/agent/diagnose", json=body(), headers=headers())
        self.assertLessEqual(seen["timeout"], 7)

    def test_timeout_signals_cancel_to_runner(self):
        observed = threading.Event()

        def waits(prompt, dsh, on_event, cancel, tools):
            if cancel.wait(3):
                observed.set()
            return RunOutcome("", None, [], "CANCELLED")
        client(waits, total_timeout_seconds=1).post("/v1/agent/diagnose", json=body(), headers=headers())
        self.assertTrue(observed.wait(3))

    def test_caller_disconnect_signals_cancel(self):
        observed = threading.Event()

        def waits(prompt, dsh, on_event, cancel, tools):
            if cancel.wait(3):
                observed.set()
            return RunOutcome("", None, [], "CANCELLED")

        async def scenario():
            diagnoser = Diagnoser(settings(), waits, EmptyCourses())
            task = asyncio.ensure_future(diagnoser.diagnose(platform_models.DiagnoseRequest.model_validate(body())))
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())
        self.assertTrue(observed.wait(3))

    def test_slot_is_held_until_runner_really_ends(self):
        release = threading.Event()

        def slow(prompt, dsh, on_event, cancel, tools):
            release.wait(5)
            return RunOutcome("好", "completed", [])

        async def scenario():
            diagnoser = Diagnoser(settings(total_timeout_seconds=1, max_concurrent_requests=1), slow, EmptyCourses())
            first = await diagnoser.diagnose(platform_models.DiagnoseRequest.model_validate(body()))
            busy = diagnoser.slots.locked()
            release.set()
            await asyncio.sleep(0.3)
            return first.status, busy, diagnoser.slots.locked()
        self.assertEqual(asyncio.run(scenario()), ("partial", True, False))


if __name__ == "__main__":
    unittest.main()
