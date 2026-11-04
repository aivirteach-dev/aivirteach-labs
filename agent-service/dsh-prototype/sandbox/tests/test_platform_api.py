"""平台接口契约：以 main 上 Server/Client 已在用的格式为准，不调用真实模型。"""
import asyncio
import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from dsh_tutor import platform_models
from dsh_tutor.platform_app import Diagnoser, Settings, create_app, normalize_base_url, normalize_usage
from dsh_tutor.platform_runtime import RunOutcome

ROOT = Path(__file__).resolve().parents[2]
MAIN_MODELS = ROOT.parent / "aivirteach_agent" / "models.py"
TOKEN = "t" * 40
# Client 的 read-tutor-stream.ts 认识的事件，加上 Server 自己处理的 result / error。
CLIENT_EVENTS = {"accepted", "started", "reasoning_started", "reasoning_finished", "context_ready", "tool_started",
                 "tool_finished", "finalization_started", "assessment_validating", "assessment_validated",
                 "answer_started", "answer_failed", "answer_finished", "assistant_delta", "result", "done", "error"}


def settings(**overrides):
    values = dict(agent_token=TOKEN, model_provider="ollama", model_base_url="http://127.0.0.1:1/v1",
                  model_name="test-model", model_api_key_set=False, model_thinking="disabled",
                  total_timeout_seconds=5, max_concurrent_requests=2, context_window=32768,
                  max_output_tokens=1024, state_dir=Path("/nonexistent"))
    values.update(overrides)
    return Settings(**values)


def body(**overrides):
    value = {"request_id": "a10beac8-d1db-4b1a-8df0-79aa8208e273", "lab_id": "lab-001", "question": "n8n 打不开？",
             "course": {"course_id": "n8n", "version": 1, "title": "n8n course"},
             "current_step": {"module_id": "m1", "lesson_id": "install-n8n", "sequence": 4, "title": "Install n8n"}}
    value.update(overrides)
    return value


def answering(*pieces, finish="completed"):
    def runner(prompt, dsh, on_delta, cancel):
        for piece in pieces:
            on_delta(piece)
        return RunOutcome("".join(pieces), finish, [])
    return runner


def failing(prompt, dsh, on_delta, cancel):
    raise RuntimeError("upstream down")


def sleeping(prompt, dsh, on_delta, cancel):
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


class ContractCopyTests(unittest.TestCase):
    def test_models_match_main_agent(self):
        spec = importlib.util.spec_from_file_location("main_agent_models", MAIN_MODELS)
        main = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = main  # 延迟注解需要按模块名解析
        try:
            spec.loader.exec_module(main)
        except ImportError as exc:  # main 面向 3.11+；在 3.10 上无法导入时只能跳过
            self.skipTest(f"main models not importable on this Python: {exc}")
        for name in ("DiagnoseRequest", "DiagnoseResponse"):
            self.assertEqual(getattr(platform_models, name).model_json_schema(),
                             getattr(main, name).model_json_schema(), name)


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
        c = client(model_name="")
        self.assertEqual(c.get("/ready").status_code, 503)
        self.assertEqual(c.post("/v1/agent/diagnose", json=body(), headers=headers()).status_code, 503)

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
    def stream(self, runner=None, **overrides):
        response = client(runner, **overrides).post("/v1/agent/diagnose/stream", json=body(), headers=headers())
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


class UsageTests(unittest.TestCase):
    def test_no_cache_split_is_null_not_zero(self):
        self.assertIsNone(normalize_usage([{"inputTokens": 71, "outputTokens": 71, "totalTokens": 142}]))
        self.assertIsNone(normalize_usage([]))

    def test_explicit_split_is_summed(self):
        usage = normalize_usage([{"cacheReadTokens": 1, "cacheMissTokens": 2, "outputTokens": 3}] * 2)
        self.assertEqual(usage.model_dump(), {"input_cache_hit_tokens": 2, "input_cache_miss_tokens": 4, "output_tokens": 6})


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
        def truncated(prompt, dsh, on_delta, cancel):
            on_delta("前半段")
            return RunOutcome("前半段", "completed", [], "MODEL_STREAM_OUTPUT_TRUNCATED")
        data = client(truncated).post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual((data["status"], data["answer"]), ("partial", "前半段"))
        self.assertIn("MODEL_STREAM_OUTPUT_TRUNCATED", data["limitations"])

    def test_run_budget_never_exceeds_total_timeout(self):
        seen = {}

        def record(prompt, dsh, on_delta, cancel):
            seen["timeout"] = dsh.total_timeout_seconds
            return RunOutcome("好", "completed", [])
        client(record, total_timeout_seconds=7).post("/v1/agent/diagnose", json=body(), headers=headers())
        self.assertLessEqual(seen["timeout"], 7)

    def test_timeout_signals_cancel_to_runner(self):
        observed = threading.Event()

        def waits(prompt, dsh, on_delta, cancel):
            if cancel.wait(3):
                observed.set()
            return RunOutcome("", None, [], "CANCELLED")
        client(waits, total_timeout_seconds=1).post("/v1/agent/diagnose", json=body(), headers=headers())
        self.assertTrue(observed.wait(3))

    def test_caller_disconnect_signals_cancel(self):
        observed = threading.Event()

        def waits(prompt, dsh, on_delta, cancel):
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

        def slow(prompt, dsh, on_delta, cancel):
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
