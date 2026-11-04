"""平台接口的 DSH 单次运行：一个请求一个 DSH 会话，可见文字片段实时转发给调用方。

不保存聊天、记忆或交付记录，这些归 Server；运行目录在请求结束后删除。
文字片段来自预算插件对 llm/stream 的拦截（DSH SDK 的公开通知只给整段回答）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from .execution_budget import ExecutionLimits
from .providers import provider_for
from .runtime import _metrics, build_budget_plugin, render_patch

STREAM_POLL_SECONDS = 0.03
MAX_STREAM_FILE_BYTES = 1_048_576


@dataclass(frozen=True)
class DshSettings:
    """可信宿主配置；学生请求不能改变模型、地址或预算。"""

    provider_kind: str  # "ollama" 或 "openai_compatible"
    base_url: str
    model: str
    api_key_env: str | None
    context_window: int
    max_output_tokens: int
    total_timeout_seconds: int
    state_dir: Path
    reasoning_off: bool = True

    def runtime_config(self) -> dict:
        limits = ExecutionLimits(
            max_model_calls=4,
            max_total_output_tokens=4 * self.max_output_tokens,
            # 预算按 UTF-8 字节/2 估算；4000 字中文问题约 6000，需留出课程资料空间。
            max_input_estimated_tokens_per_call=self.context_window - self.max_output_tokens - 512,
            max_total_input_estimated_tokens=4 * self.context_window,
            max_request_bytes=262_144,
        )
        config = {
            "model": self.model,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "total_timeout_seconds": self.total_timeout_seconds,
            "execution_limits": limits.model_dump(),
        }
        if self.reasoning_off:
            config.update(reasoning_effort="off", reasoning_off_value="none")
        if self.provider_kind == "ollama":
            config["ollama_base_url"] = self.base_url
        else:
            config["provider"] = {
                "id": "platform-model",
                "kind": "openai_compatible",
                "base_url": self.base_url,
                "auth_env": self.api_key_env,
            }
        return config


@dataclass
class RunOutcome:
    answer: str
    finish_reason: str | None
    usage: list[dict] = field(default_factory=list)
    error: str | None = None


class _StreamTail(threading.Thread):
    """读取插件逐行追加的片段文件；只接受完整的 JSON 行。"""

    def __init__(self, path: Path, on_delta: Callable[[str], None]):
        super().__init__(daemon=True)
        self.path, self.on_delta = path, on_delta
        self.usage: list[dict] = []
        self._offset = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(STREAM_POLL_SECONDS):
            self._drain()
        self._drain()

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=2)

    def _drain(self) -> None:
        if not self.path.exists() or self.path.stat().st_size > MAX_STREAM_FILE_BYTES:
            return
        with self.path.open("rb") as stream:
            stream.seek(self._offset)
            data = stream.read()
        end = data.rfind(b"\n") + 1
        self._offset += end
        for line in data[:end].splitlines():
            row = json.loads(line)
            if isinstance(row.get("delta"), str):
                self.on_delta(row["delta"])
            elif isinstance(row.get("usage"), dict):
                self.usage.append(row["usage"])


def run_once(prompt: str, settings: DshSettings, on_delta: Callable[[str], None]) -> RunOutcome:
    """阻塞执行一次 DSH 对话，应在工作线程中调用。"""
    from deepseek_harness import DeepSeekHarness

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=settings.state_dir))
    try:
        config = settings.runtime_config()
        provider = provider_for(config)
        stream_path = run_dir / "stream.jsonl"
        guard = build_budget_plugin(config, run_dir, stream_path=stream_path)
        patch = run_dir / "runtime.patch.yml"
        patch.write_text(render_patch(config, budget_plugin=guard), encoding="utf-8")
        host = urlsplit(provider.base_url).hostname
        env = {**provider.environment(), "NO_PROXY": f"{host},localhost,127.0.0.1", "no_proxy": f"{host},localhost,127.0.0.1"}
        tail = _StreamTail(stream_path, on_delta)
        tail.start()
        try:
            with DeepSeekHarness(
                dsh_home=str(run_dir / "home"),
                cwd=str(run_dir),
                profile="sdk-minimal",
                patches=(str(patch),),
                provider=provider.id,
                model=settings.model,
                env=env,
                reasoning_effort=config.get("reasoning_effort"),
                max_tokens=settings.max_output_tokens,
                request_timeout_seconds=settings.total_timeout_seconds,
            ) as harness:
                harness.start()
                result = harness.run(prompt, session_id="request-" + uuid4().hex)
        finally:
            tail.stop()
        metrics = _metrics(run_dir / "model-budget.json")
        error = "MODEL_BUDGET_EXHAUSTED" if metrics.get("budget_stop") else None
        return RunOutcome(result.final_response, result.finish_reason, tail.usage, error)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
