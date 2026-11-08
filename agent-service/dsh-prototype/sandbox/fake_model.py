"""本地测试用的假模型：OpenAI 兼容的 /v1/chat/completions 流式接口，按脚本逐次回答。

用来在不连真实模型的情况下跑通真实 DSH（文字片段、工具调用、用量、结束原因、取消）。
用量按 DeepSeek 的格式返回：prompt_tokens 含缓存命中，另给 prompt_cache_hit/miss_tokens 和
prompt_tokens_details.cached_tokens。

脚本每一项对应一次模型调用：
  {"text": ["片段", ...], "finish": "stop"}            输出文字（finish 可为 "length"）
  {"tool": "get_vm_status", "arguments": {...}}        调用一个平台工具
  {"slow": 秒数}                                       每 50ms 输出一个片段，用于测试取消和超时
  {"stall": 秒数, "text": [...]}                       先停这么久不输出任何内容，再输出文字（测试首字超时）
脚本用完后统一回答 "done"。
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

USAGE = {"prompt": 120, "cached": 80, "completion": 12}


def _chunk(model: str, delta: dict, finish: str | None = None) -> str:
    body = {"id": "fake", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"


def _usage(model: str) -> str:
    prompt, cached, completion = USAGE["prompt"], USAGE["cached"], USAGE["completion"]
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
             "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": prompt - cached,
             "prompt_tokens_details": {"cached_tokens": cached}}
    return "data: " + json.dumps({"id": "fake", "object": "chat.completion.chunk", "model": model,
                                  "choices": [], "usage": usage}) + "\n\n"


def create_app(script: list) -> FastAPI:
    app = FastAPI(title="Fake OpenAI-compatible model")
    app.state.script = list(script)
    app.state.requests = []  # 收到的请求体，测试用来检查工具列表、思考参数等

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> StreamingResponse:
        body = await request.json()
        app.state.requests.append(body)
        step = app.state.script.pop(0) if app.state.script else {"text": ["done"]}
        model = body.get("model", "fake")

        async def stream():
            yield _chunk(model, {"role": "assistant", "content": ""})
            if "tool" in step:
                name = "mcp__aivirteach_tools__" + step["tool"]
                call = {"index": 0, "id": f"call_{len(app.state.requests)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(step.get("arguments", {}))}}
                yield _chunk(model, {"tool_calls": [call]})
                finish = "tool_calls"
            elif "stall" in step:
                await asyncio.sleep(step["stall"])
                for piece in step.get("text", ["迟到的回答"]):
                    yield _chunk(model, {"content": piece})
                finish = "stop"
            elif "slow" in step:
                end = time.monotonic() + step["slow"]
                while time.monotonic() < end:
                    yield _chunk(model, {"content": "…"})
                    await asyncio.sleep(0.05)
                finish = "stop"
            else:
                for piece in step["text"]:
                    yield _chunk(model, {"content": piece})
                finish = step.get("finish", "stop")
            yield _chunk(model, {}, finish)
            yield _usage(model)
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


@contextmanager
def dropping_proxy(target_url: str, drop: int = 1) -> Iterator[str]:
    """TCP 代理：前 drop 个连接一建立就关闭（模拟网络中途切断连接），之后的连接原样转发到 target_url。"""
    host, port = target_url.rsplit("//", 1)[1].split(":")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    state = {"seen": 0}

    def pipe(source, target):
        try:
            while data := source.recv(65536):
                target.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def accept():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            state["seen"] += 1
            if state["seen"] <= drop:
                client.close()
                continue
            upstream = socket.create_connection((host, int(port)))
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """在后台线程启动 app，返回 http://127.0.0.1:<端口>。"""
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
