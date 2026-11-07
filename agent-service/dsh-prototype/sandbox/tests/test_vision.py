"""看图（阶段 2）：请求格式、两次看图（整屏 → 放大）、密钥过滤、失败和重试、复用和次数限制、接入请求流程。

看图模型用 httpx.MockTransport 模拟，不连真实模型。
"""
import base64
import io
import json
import os
import threading
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from dsh_tutor import platform_screen as screen
from dsh_tutor import platform_vision as vision
from dsh_tutor.platform_app import Settings, create_app
from dsh_tutor.platform_screen import load_screen
from dsh_tutor.platform_vision import ScreenAnalyzer, VisionSettings
from test_platform_api import EmptyCourses, answering, body, headers, settings, sse
from test_screen import capture, image_bytes

VISION = VisionSettings(model="vision-test", base_url="http://vision.test/v1", api_key_env="VISION_TEST_KEY",
                        timeout_seconds=12, max_per_hour=10)
USAGE = {"prompt_tokens": 1100, "completion_tokens": 90, "total_tokens": 1190,
         "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 1000}


def perception(**overrides):
    value = {"readable": True, "window": "browser", "app": "Firefox", "page": "n8n - Overview",
             "summary": "学生在 n8n 首页，右上角有 Create Workflow 按钮。", "visible_errors": [],
             "elements": [{"label": "Create Workflow", "x": 0.82, "y": 0.04, "width": 0.12, "height": 0.05}],
             "zoom": None, "contains_sensitive": False}
    value.update(overrides)
    return value


def reply(content, status=200, usage=USAGE, finish="stop"):
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return httpx.Response(status, json={"id": "x", "object": "chat.completion", "model": "vision-test",
                                        "choices": [{"index": 0, "finish_reason": finish,
                                                     "message": {"role": "assistant", "content": content}}],
                                        "usage": usage})


class FakeVision:
    """按顺序返回脚本里的每一项：dict / str 是模型输出，httpx.Response 原样返回，异常类会被抛出。"""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else perception()
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("simulated", request=request)
        return step if isinstance(step, httpx.Response) else reply(step)

    def bodies(self):
        return [json.loads(request.content) for request in self.requests]

    def images(self):
        result = []
        for item in self.bodies():
            url = item["messages"][1]["content"][1]["image_url"]["url"]
            result.append(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))))
        return result


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def analyzer(fake, clock=None, **overrides):
    values = {**VISION.__dict__, **overrides}
    return ScreenAnalyzer(VisionSettings(**values), transport=httpx.MockTransport(fake), clock=clock or Clock())


def loaded(width=2000, height=1250, color=None):
    raw = image_bytes(width, height) if color is None else _solid(width, height, color)
    return load_screen(capture(raw, width=width, height=height))


def _solid(width, height, color):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def analyze(tool, screen_=None, **kwargs):
    values = dict(question="找不到 Create Workflow 按钮", lesson_title="Install and Start n8n", lab_id="lab-001",
                  language="zh-CN", budget_seconds=12)
    values.update(kwargs)
    return tool.analyze(screen_ or loaded(), **values)


@mock.patch.dict(os.environ, {"VISION_TEST_KEY": "vk-" + "x" * 30})
class AnalyzerTests(unittest.TestCase):
    def test_request_format_and_result(self):
        fake = FakeVision()
        result = analyze(analyzer(fake))
        self.assertEqual((result.status, result.code, result.passes, result.cached), ("ok", None, 1, False))
        [request] = fake.requests
        self.assertEqual(str(request.url), "http://vision.test/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer vk-" + "x" * 30)
        sent = fake.bodies()[0]
        self.assertEqual((sent["model"], sent["stream"], sent["temperature"], sent["reasoning_effort"]),
                         ("vision-test", False, 0, "none"))
        system, user = sent["messages"]
        self.assertEqual(system["role"], "system")
        self.assertIn("不要抄录任何密钥", system["content"]); self.assertIn("用 中文 写", system["content"])
        self.assertEqual(user["content"][0]["type"], "text")
        self.assertIn("找不到 Create Workflow 按钮", user["content"][0]["text"])
        self.assertEqual(fake.images()[0].size, (1280, 800))  # 整屏缩略图
        findings = result.findings
        self.assertEqual(findings.highlights[0].label, "Create Workflow")
        self.assertIn("Create Workflow", findings.summary)
        [call] = result.calls
        self.assertEqual((call.phase, call.model, call.finish, call.error), ("screen", "vision-test", "stop", None))
        self.assertEqual(result.usage, [{"inputTokens": 1000, "cacheReadTokens": 100, "outputTokens": 90,
                                         "totalTokens": 1190}])

    def test_unreadable_region_is_zoomed_once_and_mapped_back(self):
        zoom = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 0.4}
        fake = FakeVision(perception(readable=False, zoom=zoom, summary="右上角的字太小。"),
                          perception(summary="放大后能看到 Create Workflow 按钮。", visible_errors=["Bad request"],
                                     elements=[{"label": "Create Workflow", "x": 0.5, "y": 0.5, "width": 0.2,
                                                "height": 0.1}]))
        result = analyze(analyzer(fake))
        self.assertEqual((result.status, result.passes), ("ok", 2))
        overview, crop = fake.images()
        self.assertEqual(crop.size, (1000, 500))  # 从 2000×1250 的原图裁出，原尺寸不缩小
        self.assertIn("右上角的字太小。", fake.bodies()[1]["messages"][1]["content"][0]["text"])
        box = result.findings.highlights[0]
        self.assertEqual((box.x, box.y, box.width, box.height), (0.75, 0.2, 0.1, 0.04))  # 换回整张图的坐标
        self.assertEqual(result.perception.visible_errors, ["Bad request"])
        self.assertEqual(result.findings.summary, "放大后能看到 Create Workflow 按钮。")

    def test_failed_zoom_keeps_the_first_result(self):
        zoom = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 0.4}
        fake = FakeVision(perception(readable=False, zoom=zoom, summary="右上角的字太小。"), httpx.Response(400))
        result = analyze(analyzer(fake))
        self.assertEqual((result.status, result.code, result.passes), ("ok", None, 2))
        self.assertEqual(result.findings.summary, "右上角的字太小。")
        self.assertEqual(len(result.usage), 1)  # 失败的那次没有用量

    def test_sensitive_screen_is_not_used(self):
        fake = FakeVision(perception(contains_sensitive=True, summary="Tavily 凭据窗口，Key 为 [已隐藏]"))
        tool = analyzer(fake)
        result = analyze(tool)
        self.assertEqual((result.status, result.code, result.findings), ("rejected", vision.SCREEN_SENSITIVE_CONTENT,
                                                                         None))
        again = analyze(tool)  # 复用的结果同样不使用
        self.assertEqual((again.status, again.cached, len(fake.requests)), ("rejected", True, 1))

    def test_secrets_in_the_description_are_redacted(self):
        fake = FakeVision(perception(summary="凭据页 api_key=abc123 和 tvly-dev-1234567890abcd、AIzaSyA1234567890abcdefghij",
                                     visible_errors=["token: ghp_secret_value_12345"],
                                     elements=[{"label": "GOCSPX-abcdefghijklmn", "x": 0.1, "y": 0.1, "width": 0.1,
                                                "height": 0.1}]))
        result = analyze(analyzer(fake))
        text = json.dumps([result.perception.summary, result.perception.visible_errors,
                           [h.label for h in result.perception.elements]], ensure_ascii=False)
        for secret in ("abc123", "tvly-dev", "AIzaSy", "ghp_secret", "GOCSPX"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_invalid_output(self):
        for content in ("我看到一个终端窗口。", json.dumps({"readable": "yes", "summary": "x"}),
                        json.dumps(perception(summary="")), json.dumps(perception(elements="none")), ""):
            result = analyze(analyzer(FakeVision(content)))
            self.assertEqual((result.status, result.code), ("failed", vision.SCREEN_ANALYSIS_INVALID), content)

    def test_fenced_json_and_bad_boxes(self):
        boxes = [{"label": "ok", "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.1},
                 {"label": "像素坐标", "x": 640, "y": 20, "width": 120, "height": 40},
                 {"label": "缺字段", "x": 0.1},
                 {"label": "超出右边", "x": 0.9, "y": 0.1, "width": 0.3, "height": 0.1},
                 *[{"label": f"多余{i}", "x": 0.01 * i, "y": 0.5, "width": 0.01, "height": 0.01} for i in range(1, 6)]]
        content = "```json\n" + json.dumps(perception(window="spaceship", elements=boxes), ensure_ascii=False) + "\n```"
        result = analyze(analyzer(FakeVision(content)))
        self.assertEqual(result.status, "ok")
        labels = [h.label for h in result.perception.elements]
        self.assertEqual(labels, ["ok", "超出右边", "多余1", "多余2", "多余3"])  # 超出的裁到图内；最多 5 个
        self.assertAlmostEqual(result.perception.elements[1].width, 0.1)
        self.assertEqual(result.perception.window, "other")

    def test_http_errors_and_retries(self):
        cases = [
            ((httpx.Response(400, json={"error": {"message": "image not supported"}}),), "failed", 1, 0),
            ((httpx.Response(503), httpx.Response(503)), "failed", 2, 1),
            ((httpx.ConnectError, perception()), "ok", 2, 1),
            ((httpx.ReadTimeout, httpx.ReadTimeout), "failed", 2, 1),
            ((httpx.Response(200, content=b"not json"),), "failed", 1, 0),
        ]
        for script, status, attempts, retries in cases:
            fake = FakeVision(*script)
            result = analyze(analyzer(fake))
            self.assertEqual((result.status, len(fake.requests), result.calls[0].retries), (status, attempts, retries),
                             script)
        timeout = analyze(analyzer(FakeVision(httpx.ReadTimeout, httpx.ReadTimeout)))
        self.assertEqual(timeout.code, vision.SCREEN_ANALYSIS_TIMEOUT)
        failed = analyze(analyzer(FakeVision(httpx.Response(400))))
        self.assertEqual((failed.code, failed.calls[0].error), (vision.SCREEN_ANALYSIS_FAILED, "http_400"))

    def test_no_time_left(self):
        fake = FakeVision()
        result = analyze(analyzer(fake), budget_seconds=0.5)
        self.assertEqual((result.status, result.code, len(fake.requests)), ("failed", vision.SCREEN_ANALYSIS_TIMEOUT, 0))

    def test_cancel_before_call(self):
        cancel = threading.Event(); cancel.set()
        fake = FakeVision()
        result = analyze(analyzer(fake), cancel=cancel)
        self.assertEqual((result.status, len(fake.requests)), ("failed", 0))

    def test_missing_api_key(self):
        fake = FakeVision()
        with mock.patch.dict(os.environ, {"VISION_TEST_KEY": ""}):
            result = analyze(analyzer(fake))
        self.assertEqual((result.status, result.calls[0].error, len(fake.requests)), ("failed", "no_api_key", 0))
        keyless = FakeVision()  # 不需要密钥的提供方（如 Ollama）
        self.assertEqual(analyze(analyzer(keyless, api_key_env=None)).status, "ok")
        self.assertNotIn("authorization", keyless.requests[0].headers)

    def test_same_screen_is_reused_for_two_minutes(self):
        clock, fake = Clock(), FakeVision()
        tool, image = analyzer(fake, clock), loaded()
        self.assertFalse(analyze(tool, image).cached)
        again = analyze(tool, image)
        self.assertEqual((again.cached, again.calls, again.usage, len(fake.requests)), (True, [], [], 1))
        analyze(tool, image, question="另一个问题")  # 问题不同，重新看
        self.assertEqual(len(fake.requests), 2)
        clock.now += vision.CACHE_SECONDS + 1
        self.assertFalse(analyze(tool, image).cached)
        self.assertEqual(len(fake.requests), 3)

    def test_per_vm_hourly_limit(self):
        clock, fake = Clock(), FakeVision()
        tool = analyzer(fake, clock, max_per_hour=2)
        images = [loaded(800, 500, (i * 40, 0, 0)) for i in range(4)]
        self.assertEqual([analyze(tool, images[i]).status for i in range(2)], ["ok", "ok"])
        limited = analyze(tool, images[2])
        self.assertEqual((limited.status, limited.code), ("rejected", vision.SCREEN_RATE_LIMITED))
        self.assertEqual(analyze(tool, images[0]).cached, True)  # 复用的结果不受限制
        self.assertEqual(analyze(tool, images[2], lab_id="lab-002").status, "ok")  # 其他 VM 不受影响
        clock.now += 3601
        self.assertEqual(analyze(tool, images[3]).status, "ok")
        self.assertEqual(len(fake.requests), 4)


class PipelineTests(unittest.TestCase):
    def app(self, fake, runner=None, **overrides):
        values = dict(model_provider="openai_compatible", model_api_key_set=True, total_timeout_seconds=40)
        values.update(overrides)
        tool = analyzer(fake, api_key_env=None)
        return TestClient(create_app(settings=settings(**values), runner=runner or answering("好"),
                                     course_repository=EmptyCourses(), screen_analyzer=tool))

    def stream(self, client, payload):
        return sse(client.post("/v1/agent/diagnose/stream", json=payload, headers=headers()).text)

    def test_screen_is_analyzed_before_assessment(self):
        fake = FakeVision()
        frames = self.stream(self.app(fake), body(screen=capture().model_dump(mode="json")))
        names = [e for e, _ in frames]
        self.assertLess(names.index(screen.SCREEN_ANALYZING), names.index(screen.SCREEN_ANALYZED))
        self.assertLess(names.index(screen.SCREEN_ANALYZED), names.index("reasoning_started"))
        event = next(d for e, d in frames if e == screen.SCREEN_ANALYZED)
        self.assertEqual((event["status"], event["passes"], event["cached"]), ("ok", 1, False))
        self.assertNotIn("code", event)
        result = frames[-2][1]["response"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["screen_findings"]["highlights"][0]["label"], "Create Workflow")
        self.assertFalse(any(code.startswith("SCREEN_") for code in result["limitations"]))
        # 看图的 token 计入返回的 usage（假的对话模型没有用量）
        self.assertEqual(result["usage"], {"input_cache_hit_tokens": 100, "input_cache_miss_tokens": 1000,
                                           "output_tokens": 90})

    def test_vision_failure_does_not_block_the_answer(self):
        fake = FakeVision(httpx.Response(400))
        frames = self.stream(self.app(fake), body(screen=capture().model_dump(mode="json")))
        event = next(d for e, d in frames if e == screen.SCREEN_ANALYZED)
        self.assertEqual((event["status"], event["code"]), ("failed", vision.SCREEN_ANALYSIS_FAILED))
        result = frames[-2][1]["response"]
        self.assertEqual((result["status"], result["answer"]), ("completed", "好"))
        self.assertIn(vision.SCREEN_ANALYSIS_FAILED, result["limitations"])
        self.assertNotIn("screen_findings", result)

    def test_not_enough_time_skips_the_vision_call(self):
        fake = FakeVision()
        frames = self.stream(self.app(fake, total_timeout_seconds=20), body(screen=capture().model_dump(mode="json")))
        event = next(d for e, d in frames if e == screen.SCREEN_ANALYZED)
        self.assertEqual((event["status"], event["code"]), ("failed", vision.SCREEN_ANALYSIS_TIMEOUT))
        self.assertEqual(fake.requests, [])

    def test_vision_call_is_its_own_span_without_content(self):
        from test_telemetry import memory_telemetry, spans_by_kind
        telemetry, exporter = memory_telemetry(capture_content=True)
        tool = analyzer(FakeVision(), api_key_env=None)
        app = create_app(settings=settings(model_provider="openai_compatible", model_api_key_set=True,
                                           total_timeout_seconds=40),
                         runner=answering("好"), course_repository=EmptyCourses(), telemetry=telemetry,
                         screen_analyzer=tool)
        TestClient(app).post("/v1/agent/diagnose", json=body(screen=capture().model_dump(mode="json")),
                             headers=headers())
        llm = spans_by_kind(exporter)["LLM"]
        [span] = [s for s in llm if s.attributes["aivirteach.phase"] == "screen"]
        self.assertEqual((span.name, span.attributes["gen_ai.request.model"]), ("chat vision-test", "vision-test"))
        self.assertEqual(span.attributes["gen_ai.usage.input_tokens"], 1100)
        dumped = json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()], ensure_ascii=False,
                            default=str)
        self.assertNotIn(capture().data[:60], dumped)            # 图片不进监控
        self.assertNotIn("右上角有 Create Workflow 按钮", dumped)  # 看图原文也不进（即使开了内容记录）

    def test_without_screen_no_vision_call(self):
        fake = FakeVision()
        frames = self.stream(self.app(fake), body())
        self.assertFalse({e for e, _ in frames} & {screen.SCREEN_ANALYZING, screen.SCREEN_ANALYZED})
        self.assertEqual(fake.requests, [])


class SettingsTests(unittest.TestCase):
    def test_vision_is_off_unless_a_model_name_is_set(self):
        with mock.patch.dict(os.environ, {"AIVIRTEACH_MODEL_BASE_URL": "https://api.deepseek.com/",
                                          "AIVIRTEACH_MODEL_API_KEY": "k"}, clear=True):
            self.assertFalse(Settings.from_env().vision().enabled)
        with mock.patch.dict(os.environ, {"AIVIRTEACH_MODEL_BASE_URL": "https://api.deepseek.com/",
                                          "AIVIRTEACH_MODEL_API_KEY": "k",
                                          "AIVIRTEACH_VISION_MODEL_NAME": "deepseek-flash"}, clear=True):
            config = Settings.from_env().vision()
            self.assertEqual((config.enabled, config.base_url, config.api_key_env, config.model),
                             (True, "https://api.deepseek.com/v1", "AIVIRTEACH_MODEL_API_KEY", "deepseek-flash"))

    def test_vision_can_use_its_own_provider(self):
        with mock.patch.dict(os.environ, {"AIVIRTEACH_MODEL_BASE_URL": "https://api.deepseek.com/",
                                          "AIVIRTEACH_MODEL_API_KEY": "k",
                                          "AIVIRTEACH_VISION_MODEL_NAME": "qwen2.5vl:7b",
                                          "AIVIRTEACH_VISION_MODEL_BASE_URL": "http://10.0.0.5:11434",
                                          "AIVIRTEACH_VISION_MODEL_API_KEY": "vk",
                                          "AIVIRTEACH_VISION_TIMEOUT": "99",
                                          "AIVIRTEACH_SCREEN_MAX_PER_HOUR": "0"}, clear=True):
            config = Settings.from_env().vision()
        self.assertEqual((config.base_url, config.api_key_env, config.timeout_seconds, config.max_per_hour),
                         ("http://10.0.0.5:11434/v1", "AIVIRTEACH_VISION_MODEL_API_KEY", 30, 0))

    def test_bad_vision_url_disables_vision(self):
        with mock.patch.dict(os.environ, {"AIVIRTEACH_VISION_MODEL_NAME": "m",
                                          "AIVIRTEACH_VISION_MODEL_BASE_URL": "http://host/api/chat"}, clear=True):
            self.assertFalse(Settings.from_env().vision().enabled)


if __name__ == "__main__":
    unittest.main()
