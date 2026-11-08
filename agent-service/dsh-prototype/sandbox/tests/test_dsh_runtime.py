"""真实 DSH 运行时 + 假模型（sandbox/fake_model.py）+ 假 Gateway：验证插件转发、工具、预算、取消和时间戳。

不连真实模型或真实 Gateway。DSH 运行时未安装时整组跳过；node 不在 PATH 时跳过插件单元测试。
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from dsh_tutor import dsh_patch, platform_models
from dsh_tutor.platform_app import normalize_usage
from dsh_tutor.platform_mcp import ToolContext
from dsh_tutor.platform_runtime import DshSettings, run_once
from sandbox import fake_gateway, fake_model

ROOT = Path(__file__).resolve().parents[2]
KEY_ENV = "AIVIRTEACH_TEST_MODEL_KEY"
GATEWAY_TOKEN = "g" * 40

try:
    import deepseek_harness  # noqa: F401
    HAVE_DSH = True
except ImportError:
    HAVE_DSH = False


def dsh_settings(base_url, state_dir, **overrides):
    values = dict(provider_kind="openai_compatible", base_url=base_url + "/v1", model="fake-model",
                  api_key_env=KEY_ENV, context_window=32768, max_output_tokens=1024, total_timeout_seconds=30,
                  state_dir=Path(state_dir), max_model_calls=5)
    values.update(overrides)
    return DshSettings(**values)


class Recorder:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, kind, data):
        with self.lock:
            self.events.append((kind, data))

    def kinds(self, kind):
        return [data for k, data in self.events if k == kind]


@unittest.skipUnless(HAVE_DSH, "deepseek-harness runtime is not installed")
class DshRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ[KEY_ENV] = "fake-key-0123456789"
        os.environ["AIVIRTEACH_DIAGNOSTIC_TOKEN"] = GATEWAY_TOKEN

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="dsh-test-")
        self.addCleanup(shutil.rmtree, self.state, True)

    def run_script(self, script, tools=None, cancel=None, on_event=None, **overrides):
        app = fake_model.create_app(script)
        recorder = on_event or Recorder()
        with fake_model.serve(app) as url:
            outcome = run_once("问题", dsh_settings(url, self.state, **overrides), recorder, cancel, tools(url) if tools else None)
        self.assertEqual(os.listdir(self.state), [], "run directory must be removed")
        return outcome, recorder, app.state.requests

    def tools(self, gateway_url, allowed=("get_vm_status",)):
        return ToolContext(lab_id="lab-001", scope=platform_models.DiagnosticScope(allowed_tools=set(allowed)),
                           gateway_url=gateway_url, gateway_token=GATEWAY_TOKEN, events_path="",
                           deadline_unix=time.time() + 30)

    def test_text_is_streamed_with_usage_and_timings(self):
        outcome, events, requests = self.run_script([{"text": ["你好", "，世界"]}])
        self.assertEqual((outcome.answer, outcome.finish_reason, outcome.error), ("你好，世界", "completed", None))
        self.assertEqual("".join(d["text"] for d in events.kinds("delta")), "你好，世界")
        [call] = outcome.calls
        self.assertEqual(call.finish, "stop")
        self.assertTrue(call.start_ms <= call.first_delta_ms <= call.end_ms)
        usage = normalize_usage(outcome.usage)
        prompt, cached, completion = (fake_model.USAGE[k] for k in ("prompt", "cached", "completion"))
        self.assertEqual((usage.input_cache_hit_tokens, usage.input_cache_miss_tokens, usage.output_tokens),
                         (cached, prompt - cached, completion))
        self.assertEqual(len(requests), 1)
        self.assertFalse(requests[0].get("tools"))
        self.assertEqual(requests[0].get("reasoning_effort"), "none")

    def test_rules_go_to_the_system_message_and_data_to_the_user_message(self):
        _, _, requests = self.run_script([{"text": ["好"]}], system_prompt="助教规则：只读检查。")
        messages = requests[0]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "助教规则：只读检查。"})
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"][0], "问题")
        self.assertEqual(sum(m["role"] == "system" for m in messages), 1)

    def test_tool_call_reaches_gateway_and_is_timed_between_model_calls(self):
        with fake_model.serve(fake_gateway.app) as gateway:
            outcome, events, requests = self.run_script(
                [{"tool": "get_vm_status"}, {"text": ["虚拟机在运行 [obs-001]"]}], tools=lambda _: self.tools(gateway))
        self.assertIsNone(outcome.error)
        self.assertEqual([(e["id"], e["summary"]) for e in outcome.evidence],
                         [("obs-001", "VM status and reported IPv4 addresses were collected.")])
        self.assertEqual([(t["tool"], t["status"]) for t in outcome.tool_trace], [("get_vm_status", "ok")])
        self.assertEqual([d["name"] for d in events.kinds("tool_event")], ["tool_started", "tool_finished"])
        self.assertEqual(outcome.tool_events, events.kinds("tool_event"))
        self.assertEqual([t["function"]["name"] for t in requests[0]["tools"]], ["mcp__aivirteach_tools__get_vm_status"])
        first, second = outcome.calls
        started = events.kinds("tool_event")[0]["t"]
        self.assertTrue(first.end_ms <= started <= second.start_ms)
        self.assertNotIn(GATEWAY_TOKEN, repr(events.events))

    def test_model_call_limit_stops_the_run(self):
        with fake_model.serve(fake_gateway.app) as gateway:
            outcome, _, requests = self.run_script([{"tool": "get_vm_status"}, {"text": ["不会到这里"]}],
                                                   tools=lambda _: self.tools(gateway), max_model_calls=1)
        self.assertEqual((outcome.error, outcome.budget_stop), ("MODEL_BUDGET_EXHAUSTED", "model_calls"))
        self.assertEqual(len(requests), 1)

    def test_dropped_connection_is_retried_once(self):
        app = fake_model.create_app([{"text": ["重试后成功"]}])
        with fake_model.serve(app) as url, fake_model.dropping_proxy(url, drop=1) as proxy:
            outcome = run_once("问题", dsh_settings(proxy, self.state), Recorder())
        self.assertEqual((outcome.answer, outcome.error), ("重试后成功", None))
        [call] = outcome.calls
        self.assertEqual((call.retries, call.finish), (1, "stop"))
        self.assertEqual(len(app.state.requests), 1)  # 被切断的那次没有到达模型

    def test_connection_dropped_twice_is_not_retried_again(self):
        app = fake_model.create_app([{"text": ["不会到这里"]}])
        with fake_model.serve(app) as url, fake_model.dropping_proxy(url, drop=5) as proxy:
            outcome = run_once("问题", dsh_settings(proxy, self.state), Recorder())
        [call] = outcome.calls
        self.assertEqual((call.retries, call.finish, outcome.answer), (1, "error", ""))
        self.assertEqual(outcome.error, "MODEL_PROVIDER_ERROR")
        self.assertEqual(len(app.state.requests), 0)

    def test_silent_call_is_abandoned_and_retried_once(self):
        """服务器测试里一次调用一直不返回，用光了总时限：首字超时后放弃这次请求，同一次调用重试一次。"""
        outcome, _, requests = self.run_script([{"stall": 4, "text": ["太迟了"]}, {"text": ["重试后成功"]}],
                                               first_token_seconds=1)
        self.assertEqual((outcome.answer, outcome.error), ("重试后成功", None))
        [call] = outcome.calls
        self.assertEqual((call.retries, call.finish), (1, "stop"))
        self.assertEqual(len(requests), 2)

    def test_second_silent_attempt_fails_the_call(self):
        started = time.monotonic()
        outcome, _, requests = self.run_script([{"stall": 4}, {"stall": 4}], first_token_seconds=1)
        self.assertLess(time.monotonic() - started, 3.9)  # 不等上游，两次各 1 秒就放弃
        [call] = outcome.calls
        self.assertEqual((call.retries, call.error, outcome.answer), (1, "stream_error", ""))
        self.assertEqual(outcome.error, "MODEL_PROVIDER_ERROR")  # 不能当成模型的正常（空）输出
        self.assertEqual(len(requests), 2)

    def test_length_finish_is_reported_as_truncated(self):
        outcome, _, _ = self.run_script([{"text": ["前半段"], "finish": "length"}])
        self.assertEqual(outcome.error, "MODEL_STREAM_OUTPUT_TRUNCATED")

    def test_cancel_stops_a_streaming_call(self):
        cancel = threading.Event()
        recorder = Recorder()

        def on_event(kind, data):
            recorder(kind, data)
            if kind == "delta":
                cancel.set()
        started = time.monotonic()
        outcome, _, _ = self.run_script([{"slow": 10}], cancel=cancel, on_event=on_event)
        self.assertEqual(outcome.error, "CANCELLED")
        self.assertLess(time.monotonic() - started, 6)


class PatchTests(unittest.TestCase):
    def test_missing_handshake_means_budget_plugin_not_mounted(self):
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(RuntimeError):
                dsh_patch.read_budget_stop(Path(run_dir))

    def test_provider_rejects_credentials_and_reserved_key_names(self):
        for url in ("https://user:pw@h/v1", "https://h/api", "ftp://h/v1"):
            with self.assertRaises(ValueError):
                dsh_patch.Provider("p", url, "KEY")
        for name in ("DSH_KEY", "OLLAMA_PLACEHOLDER_KEY", "lower"):
            with self.assertRaises(ValueError):
                dsh_patch.Provider("p", "https://h/v1", name)

    def test_reasoning_off_sends_none_only_when_thinking_is_disabled(self):
        provider = dsh_patch.Provider("p", "https://h/v1", "KEY")

        def efforts(off):
            plugin = dsh_patch.provider_plugin(provider, model="m", context_window=8192, max_output_tokens=512,
                                               reasoning_off=off)
            return plugin["config"]["providers"]["p"]["models"][0].get("reasoningEfforts")
        self.assertEqual(efforts(True), {"off": "none", "low": "low"})
        self.assertIsNone(efforts(False))

    def test_base_patch_disables_dsh_builtin_tools(self):
        text = dsh_patch.BASE_PATCH.read_text(encoding="utf-8-sig")
        for plugin in ("tool-fs", "tool-working-directory", "persistent-bash", "mcp-resources", "llm-retry"):
            self.assertIn(f"- id: {plugin}\n  disabled: true", text)

    @unittest.skipUnless(HAVE_DSH, "deepseek-harness runtime is not installed")
    def test_dsh_official_telemetry_is_disabled(self):
        import deepseek_harness
        seen = {}

        class Capture:
            def __init__(self, **kwargs):
                seen.update(kwargs)
                raise RuntimeError("captured")
        original = getattr(deepseek_harness, "DeepSeekHarness", None)
        deepseek_harness.DeepSeekHarness = Capture
        os.environ[KEY_ENV] = "fake-key-0123456789"
        try:
            with tempfile.TemporaryDirectory() as state:
                with self.assertRaises(RuntimeError):
                    run_once("q", dsh_settings("http://127.0.0.1:1", state), lambda *_: None)
        finally:
            deepseek_harness.DeepSeekHarness = original
        self.assertEqual(seen["env"]["DSH_TELEMETRY_DISABLED"], "1")

    def test_fake_gateway_matches_real_gateway_wording(self):
        source = ROOT.parent.parent / "diagnostic-gateway" / "diagnostic_gateway.py"
        if not source.exists():
            self.skipTest("diagnostic-gateway is not in this checkout")
        text = source.read_text(encoding="utf-8")
        for tool in ("get_vm_status", "get_guest_agent_status"):
            self.assertIn(f'return "{fake_gateway.CANNED[tool][0]}"', text, tool)

    @unittest.skipUnless(shutil.which("node"), "node is not on PATH")
    def test_budget_plugin_unit_tests(self):
        result = subprocess.run([shutil.which("node"), "--test", "sandbox/tests/test_model_budget.mjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
