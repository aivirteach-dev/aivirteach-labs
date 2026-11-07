"""虚拟机画面（阶段 1）：请求里的 screen、图片检查、缩略和裁剪、返回里的 screen_findings。不调用任何模型。"""
import asyncio
import base64
import io
import json
import struct
import time
import unittest
import zlib

from PIL import Image
from pydantic import ValidationError

from dsh_tutor import platform_screen as screen
from dsh_tutor.platform_screen import (BodySizeLimit, Highlight, ScreenCapture, ScreenDiagnoseRequest,
                                       ScreenDiagnoseResponse, ScreenFindings, ScreenRejected, load_screen)
from test_platform_api import CLIENT_EVENTS, answering, body, client, headers, sse

MAIN_RESPONSE_KEYS = ["request_id", "status", "answer", "diagnosis", "course_alignment", "evidence",
                      "suggested_actions", "limitations", "tool_trace", "usage"]
SCREEN_EVENTS = {screen.SCREEN_ANALYZING, screen.SCREEN_ANALYZED, screen.SCREEN_REQUESTED}


def image_bytes(width=1280, height=800, fmt="PNG", **save):
    image = Image.new("RGB", (width, height), (40, 44, 52))
    image.paste((230, 90, 60), (width // 2, height // 4, width // 2 + 120, height // 4 + 40))  # 一个“按钮”
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **save)
    return buffer.getvalue()


def capture(raw=None, mime="image/png", width=1280, height=800, construct=False, **extra):
    values = dict(mime_type=mime, data=base64.b64encode(raw if raw is not None else image_bytes(width, height)).decode(),
                  width=width, height=height, captured_at="2026-11-07T08:00:00Z")
    values.update(extra)
    return ScreenCapture.model_construct(**values) if construct else ScreenCapture.model_validate(values)


def png_header_only(width, height):
    """只有文件头的 PNG：声明的尺寸很大，实际没有像素数据（模拟特制的“解压炸弹”）。"""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IEND", b"")


def rejected(test, item):
    with test.assertRaises(ScreenRejected) as caught:
        load_screen(item)
    return caught.exception.code


class LoadScreenTests(unittest.TestCase):
    def test_png_jpeg_webp_are_accepted(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            loaded = load_screen(capture(image_bytes(fmt=fmt), mime=mime))
            self.assertEqual((loaded.size, loaded.image.mode), ((1280, 800), "RGB"), fmt)
            self.assertEqual(len(loaded.sha256), 64)

    def test_each_problem_has_its_own_code(self):
        cases = {
            screen.SCREEN_INVALID_BASE64: capture(construct=True, data="data:image/png;base64,AAAA"),
            screen.SCREEN_TOO_LARGE: capture(b"\x89PNG\r\n\x1a\n" + bytes(screen.MAX_SCREEN_BYTES), construct=True),
            screen.SCREEN_FORMAT_MISMATCH: capture(image_bytes(fmt="PNG"), mime="image/jpeg"),
            screen.SCREEN_DIMENSIONS_MISMATCH: capture(image_bytes(1280, 800), width=1280, height=720),
            screen.SCREEN_DIMENSIONS_TOO_LARGE: capture(image_bytes(2560, 1700), width=2560, height=1700),
            screen.SCREEN_UNREADABLE: capture(image_bytes()[:400]),
        }
        for code, item in cases.items():
            self.assertEqual(rejected(self, item), code)

    def test_huge_declared_size_is_rejected_without_decoding(self):
        started = time.monotonic()
        for side in (3000, 50_000):
            item = capture(png_header_only(side, side), construct=True, width=side, height=side)
            self.assertEqual(rejected(self, item), screen.SCREEN_DIMENSIONS_TOO_LARGE)
        self.assertLess(time.monotonic() - started, 1)

    def test_animated_webp_is_rejected(self):
        frames = [Image.new("RGB", (400, 300), color) for color in ((255, 0, 0), (0, 0, 255))]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
        item = capture(buffer.getvalue(), mime="image/webp", width=400, height=300)
        self.assertEqual(rejected(self, item), screen.SCREEN_ANIMATED)

    def test_overview_and_crop(self):
        loaded = load_screen(capture(image_bytes(2000, 1250), width=2000, height=1250))
        overview = Image.open(io.BytesIO(loaded.overview()))
        self.assertEqual((overview.format, overview.size), ("PNG", (1280, 800)))
        crop = Image.open(io.BytesIO(loaded.crop(Highlight(label="按钮", x=0.5, y=0.25, width=0.25, height=0.2))))
        self.assertEqual(crop.size, (500, 250))  # 原图像素，不缩小也不放大
        self.assertEqual(crop.getpixel((10, 10)), (230, 90, 60))
        with self.assertRaises(ValueError):
            loaded.crop(Highlight(label="太小", x=0.1, y=0.1, width=0.005, height=0.005))
        small = load_screen(capture(image_bytes(640, 400), width=640, height=400))
        self.assertEqual(Image.open(io.BytesIO(small.overview())).size, (640, 400))  # 小图不放大
        self.assertTrue(screen.png_data_url(b"x").startswith("data:image/png;base64,"))


class ModelTests(unittest.TestCase):
    def test_capture_shape_is_strict(self):
        for bad in (dict(width=100), dict(mime_type="image/gif"), dict(source="browser"), dict(extra="x"),
                    dict(data="A" * (screen.MAX_SCREEN_DATA_CHARS + 4))):
            values = {**capture().model_dump(mode="json"), **bad}
            with self.assertRaises(ValidationError, msg=bad):
                ScreenCapture.model_validate(values)

    def test_capture_data_is_not_in_repr(self):
        item = capture()
        self.assertNotIn(item.data[:40], repr(item))
        self.assertNotIn(item.data[:40], repr(ScreenDiagnoseRequest.model_validate({**body(), "screen": item})))

    def test_highlights_stay_inside_the_image(self):
        with self.assertRaises(ValidationError):
            Highlight(label="越界", x=0.9, y=0.1, width=0.2, height=0.1)
        with self.assertRaises(ValidationError):
            ScreenFindings(summary="x", highlights=[Highlight(label="a", x=0, y=0, width=0.1, height=0.1)] * 6)

    def test_findings_are_omitted_when_absent(self):
        base = dict(request_id=body()["request_id"], status="completed", answer="好", diagnosis={},
                    course_alignment={}, evidence=[], suggested_actions=[], limitations=[], tool_trace=[])
        self.assertEqual(list(ScreenDiagnoseResponse(**base).model_dump(mode="json")), MAIN_RESPONSE_KEYS)
        findings = ScreenFindings(summary="n8n 编辑器", highlights=[Highlight(label="Create Workflow", x=0.8, y=0.05,
                                                                          width=0.15, height=0.06)])
        dumped = ScreenDiagnoseResponse(**base, screen_findings=findings).model_dump(mode="json")
        self.assertEqual(dumped["screen_findings"]["highlights"][0]["label"], "Create Workflow")


class ApiTests(unittest.TestCase):
    def with_screen(self, item=None, **extra):
        return body(screen=(item or capture()).model_dump(mode="json"), **extra)

    def test_without_screen_nothing_changes(self):
        data = client().post("/v1/agent/diagnose", json=body(), headers=headers()).json()
        self.assertEqual(list(data), MAIN_RESPONSE_KEYS)
        self.assertFalse(any(code.startswith("SCREEN_") for code in data["limitations"]))
        names = [e for e, _ in sse(client().post("/v1/agent/diagnose/stream", json=body(), headers=headers()).text)]
        self.assertFalse(set(names) & SCREEN_EVENTS)

    def test_valid_screen_is_checked_but_not_analyzed_yet(self):
        calls = []
        frames = sse(client(answering("好", calls=calls)).post("/v1/agent/diagnose/stream", json=self.with_screen(),
                                                                headers=headers()).text)
        names = [e for e, _ in frames]
        self.assertLess(names.index(screen.SCREEN_ANALYZED), names.index("reasoning_started"))
        event = next(d for e, d in frames if e == screen.SCREEN_ANALYZED)
        self.assertEqual((event["status"], event["code"]), ("skipped", screen.SCREEN_NOT_ANALYZED))
        self.assertTrue(set(names) - SCREEN_EVENTS <= CLIENT_EVENTS)
        result = frames[-2][1]["response"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "好\n\n（这次没能查看你发来的虚拟机画面，以上回答没有参考画面。）")
        self.assertIn(screen.SCREEN_NOT_ANALYZED, result["limitations"])
        self.assertNotIn("screen_findings", result)
        data = capture().data
        self.assertFalse(any(data[:100] in str(call) for call in calls))  # 截图不会进任何提示词

    def test_invalid_screen_does_not_block_the_answer(self):
        item = capture(image_bytes(1280, 800), width=1280, height=720)
        frames = sse(client().post("/v1/agent/diagnose/stream", json=self.with_screen(item), headers=headers()).text)
        event = next(d for e, d in frames if e == screen.SCREEN_ANALYZED)
        self.assertEqual((event["status"], event["code"]), ("rejected", screen.SCREEN_DIMENSIONS_MISMATCH))
        result = frames[-2][1]["response"]
        self.assertEqual(result["status"], "completed")
        self.assertIn(screen.SCREEN_DIMENSIONS_MISMATCH, result["limitations"])

    def test_422_does_not_echo_screen_data(self):
        payload = self.with_screen()
        payload["screen"]["width"] = 50
        response = client().post("/v1/agent/diagnose", json=payload, headers=headers())
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(payload["screen"]["data"][:40], response.text)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "screen", "width"])
        # 其他字段的 422 与 FastAPI 默认（也就是 main）相同，仍带 input
        other = client().post("/v1/agent/diagnose", json=body(lab_id="bad id"), headers=headers()).json()
        self.assertEqual(other["detail"][0]["input"], "bad id")

    def test_oversized_body_is_rejected_before_parsing(self):
        response = client().post("/v1/agent/diagnose", content=b"{" + b" " * (screen.MAX_REQUEST_BODY_BYTES + 1),
                                 headers={**headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_openapi_documents_the_extension(self):
        spec = client().get("/openapi.json").json()["components"]["schemas"]
        self.assertIn("screen", spec["ScreenDiagnoseRequest"]["properties"])
        self.assertIn("screen_findings", spec["ScreenDiagnoseResponse"]["properties"])


class BodySizeLimitTests(unittest.TestCase):
    """不带 Content-Length（分块发送）时也按实际读到的字节数限制，并把读到的内容原样交给应用。"""

    def run_limit(self, chunks, max_bytes=10):
        seen, sent = {}, []

        async def app(scope, receive, send):
            seen["body"] = (await receive())["body"]
            seen["next"] = (await receive())["type"]

        async def receive():
            return chunks.pop(0) if chunks else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "headers": []}
        asyncio.run(BodySizeLimit(app, max_bytes)(scope, receive, send))
        return seen, sent

    def test_small_chunked_body_is_replayed(self):
        seen, sent = self.run_limit([{"type": "http.request", "body": b"abc", "more_body": True},
                                     {"type": "http.request", "body": b"def", "more_body": False}])
        self.assertEqual(seen, {"body": b"abcdef", "next": "http.disconnect"})
        self.assertEqual(sent, [])

    def test_large_chunked_body_gets_413(self):
        seen, sent = self.run_limit([{"type": "http.request", "body": b"x" * 8, "more_body": True},
                                     {"type": "http.request", "body": b"x" * 8, "more_body": False}])
        self.assertEqual(seen, {})
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(json.loads(sent[1]["body"]), {"detail": "Request body is too large."})


if __name__ == "__main__":
    unittest.main()
