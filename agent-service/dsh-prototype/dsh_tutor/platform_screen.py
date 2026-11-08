"""学生授权的虚拟机画面（截图）：请求里的可选字段 screen、图片检查、缩略和裁剪、返回里的可选字段 screen_findings。

这是在 main 接口上的扩展（platform_models.py 仍是 main 的原样副本）：
- Server 不发 screen 时，请求处理和返回内容与 main 完全相同，返回里也不会出现 screen_findings；
- Server 用 zod 校验返回时不认识的字段会被去掉，所以多出 screen_findings 也不会出错。

截图只在内存里处理：不写文件、不写日志、不进监控，请求结束即丢弃。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import Field, model_serializer, model_validator

from .platform_models import DiagnoseRequest, DiagnoseResponse, StrictModel

MAX_SCREEN_BYTES = 3 * 1024 * 1024          # 解码后的图片文件大小
MAX_SCREEN_SIDE = 2560                       # 单边像素
MIN_SCREEN_SIDE = 200
MAX_SCREEN_PIXELS = 2560 * 1600              # 总像素：防止特制图片在解码时耗尽内存
MAX_SCREEN_DATA_CHARS = 4 * ((MAX_SCREEN_BYTES + 2) // 3)
OVERVIEW_SIDE = 1280                         # 交给看图模型的整屏缩略图的长边
MIN_CROP_SIDE = 32
# 普通请求只有几十 KB；带截图时 base64 约 4 MB。超过这个大小的请求体在解析前就拒绝。
MAX_REQUEST_BODY_BYTES = 6 * 1024 * 1024

# 没有新增进度事件：带不带截图，SSE 事件都与 main 相同。截图没用上的原因只写进 limitations。
# 原因码：
SCREEN_INVALID_BASE64 = "SCREEN_INVALID_BASE64"
SCREEN_TOO_LARGE = "SCREEN_TOO_LARGE"
SCREEN_FORMAT_MISMATCH = "SCREEN_FORMAT_MISMATCH"
SCREEN_DIMENSIONS_TOO_LARGE = "SCREEN_DIMENSIONS_TOO_LARGE"
SCREEN_DIMENSIONS_MISMATCH = "SCREEN_DIMENSIONS_MISMATCH"
SCREEN_ANIMATED = "SCREEN_ANIMATED"
SCREEN_UNREADABLE = "SCREEN_UNREADABLE"
SCREEN_NOT_ANALYZED = "SCREEN_NOT_ANALYZED"  # 看图功能还没启用（阶段 2 之前一律如此）

_FORMATS = {  # MIME → (Pillow 格式名, 文件头判断)
    "image/png": ("PNG", lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("JPEG", lambda raw: raw.startswith(b"\xff\xd8\xff")),
    "image/webp": ("WEBP", lambda raw: raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"),
}


class ScreenCapture(StrictModel):
    """Client 在学生同意后从 Guacamole 导出的当前 VM 桌面画面。"""

    source: Literal["vm_console"] = "vm_console"
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: str = Field(min_length=1, max_length=MAX_SCREEN_DATA_CHARS, repr=False,
                      description="Standard base64 of the image file, without a data: prefix.")
    width: int = Field(ge=MIN_SCREEN_SIDE, le=MAX_SCREEN_SIDE)
    height: int = Field(ge=MIN_SCREEN_SIDE, le=MAX_SCREEN_SIDE)
    captured_at: datetime


class Highlight(StrictModel):
    """回答里要在截图上标出的位置，坐标是相对整张图的比例（0～1）。"""

    label: str = Field(min_length=1, max_length=80)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def inside_image(self) -> "Highlight":
        if self.x + self.width > 1.0001 or self.y + self.height > 1.0001:
            raise ValueError("highlight must stay inside the image")
        return self


class ScreenFindings(StrictModel):
    summary: str = Field(default="", max_length=1_000)
    highlights: list[Highlight] = Field(default_factory=list, max_length=5)


class ScreenDiagnoseRequest(DiagnoseRequest):
    screen: Optional[ScreenCapture] = None


class ScreenDiagnoseResponse(DiagnoseResponse):
    screen_findings: Optional[ScreenFindings] = None

    @model_serializer(mode="wrap")
    def _omit_absent_findings(self, handler):
        data = handler(self)
        if self.screen_findings is None and isinstance(data, dict):
            data.pop("screen_findings", None)  # 没有截图时返回内容与 main 一字不差
        return data


class ScreenRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class LoadedScreen:
    """检查通过、已解码的截图（RGB）。只在一次请求内使用。"""

    image: Image.Image = field(repr=False)
    sha256: str
    byte_size: int

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def overview(self, max_side: int = OVERVIEW_SIDE) -> bytes:
        """整屏缩略图（PNG，保持比例，不放大）。"""
        image = self.image.copy()
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return _png(image)

    def crop(self, region: Highlight, max_side: int = OVERVIEW_SIDE) -> bytes:
        """按比例坐标从原图裁出一块（PNG，不放大）；区域太小时报 ValueError。"""
        width, height = self.image.size
        left, top = round(region.x * width), round(region.y * height)
        right = min(width, round((region.x + region.width) * width))
        bottom = min(height, round((region.y + region.height) * height))
        if right - left < MIN_CROP_SIDE or bottom - top < MIN_CROP_SIDE:
            raise ValueError("crop region is too small")
        image = self.image.crop((left, top, right, bottom))
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        return _png(image)

    def close(self) -> None:
        self.image.close()


def load_screen(capture: ScreenCapture) -> LoadedScreen:
    """解码并检查截图；不合格时抛 ScreenRejected（带原因码）。先只读文件头判断尺寸，再完整解码。"""
    try:
        raw = base64.b64decode(capture.data, validate=True)
    except (binascii.Error, ValueError):
        raise ScreenRejected(SCREEN_INVALID_BASE64) from None
    if len(raw) > MAX_SCREEN_BYTES:
        raise ScreenRejected(SCREEN_TOO_LARGE)
    pil_format, looks_right = _FORMATS[capture.mime_type]
    if not looks_right(raw):
        raise ScreenRejected(SCREEN_FORMAT_MISMATCH)
    try:
        with Image.open(io.BytesIO(raw), formats=[pil_format]) as image:
            width, height = image.size
            if max(width, height) > MAX_SCREEN_SIDE or width * height > MAX_SCREEN_PIXELS:
                raise ScreenRejected(SCREEN_DIMENSIONS_TOO_LARGE)
            if (width, height) != (capture.width, capture.height):
                raise ScreenRejected(SCREEN_DIMENSIONS_MISMATCH)
            if getattr(image, "n_frames", 1) > 1:
                raise ScreenRejected(SCREEN_ANIMATED)
            image.load()
            rgb = image.convert("RGB")
    except ScreenRejected:
        raise
    except Image.DecompressionBombError:  # 文件头声明的尺寸大到 Pillow 在打开时就拒绝
        raise ScreenRejected(SCREEN_DIMENSIONS_TOO_LARGE) from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ScreenRejected(SCREEN_UNREADABLE) from None
    return LoadedScreen(image=rgb, sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))


def png_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def validation_error_handler(_request: Any, exc: RequestValidationError) -> JSONResponse:
    """与 FastAPI 默认的 422 相同，只是 screen 下的错误不回显输入（截图数据可能有几 MB，也属于学生隐私）。"""
    errors = []
    for error in exc.errors():
        error = dict(error)
        if "screen" in error.get("loc", ()):
            error.pop("input", None)
            error.pop("ctx", None)
        errors.append(error)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


class BodySizeLimit:
    """ASGI 中间件：POST 请求体超过上限时直接返回 413，不交给 JSON 解析。"""

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return
        body, more = bytearray(), True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            more = message.get("more_body", False)
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()  # 之后的读取用来发现断开（SSE 依赖它取消生成）

        await self.app(scope, replay, send)

    async def _reject(self, send: Any) -> None:
        payload = json.dumps({"detail": "Request body is too large."}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})
