"""平台接口的 DSH 单次运行：一个请求一个 DSH 会话，过程事件实时交给调用方。

不保存聊天、记忆或交付记录，这些归 Server；运行目录在请求结束后删除。
- 文字片段、用量、结束原因：预算插件拦截 llm/stream 后写入事件文件（SDK 公开通知只给整段回答）。
- 工具事件、证据、调用记录：platform_mcp 工具桥写入同一个事件文件。
每条事件带毫秒时间戳，RunOutcome.calls 据此给出每次模型调用的耗时和首字延迟（供监控使用）。

提问可以是 LatePrompt：DSH 启动（起进程、加载插件、连工具桥，约 1.5～2.5 秒）的同时在当前线程里准备提问
（预先检查、看图），两边都好了再发出。系统消息在启动时就要定下来，所以不能依赖准备的结果。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from .dsh_patch import FIRST_TOKEN_SECONDS, Provider, budget_plugin, provider_plugin, read_budget_stop, render_patch, tool_plugin
from .platform_mcp import Prefetch, ToolContext, provider_tool_name

STREAM_POLL_SECONDS = 0.03
MAX_STREAM_FILE_BYTES = 4_194_304
MAX_OUTPUT_BYTES = 65_536  # 约 2 万中文字符，高于回答上限 12000 字符
TRUNCATED_FINISHES = {"length", "max-tokens"}

# on_event(kind, data)：kind 为 start / delta / block / finish / error / tool_event
EventCallback = Callable[[str, dict], None]


@dataclass
class LatePrompt:
    """和 DSH 启动同时准备的提问：build() 返回 (用户消息, 预先检查的状态或 None)。"""

    build: Callable[[], tuple]


def prompt_text(prompt) -> str:
    """测试和 fake 提供方用：直接拿到最终的用户消息（LatePrompt 当场准备）。"""
    return prompt.build()[0] if isinstance(prompt, LatePrompt) else prompt


@dataclass(frozen=True)
class DshSettings:
    """可信宿主配置；学生请求不能改变模型、地址或预算。"""

    provider_kind: str  # "ollama" 或 "openai_compatible"
    base_url: str
    model: str
    api_key_env: Optional[str]
    context_window: int
    max_output_tokens: int
    total_timeout_seconds: int
    state_dir: Path
    reasoning_off: bool = True
    max_model_calls: int = 4  # 本次 DSH 会话最多调用模型几次
    # 助教规则，作为模型看到的系统消息（经 DSH 的 DSH_SYSTEM_PROMPT）；为空时是 DSH 默认的“软件工程师助手”。
    system_prompt: str = ""
    first_token_seconds: int = FIRST_TOKEN_SECONDS  # 单次调用多久没输出内容就重试一次（0 表示不限制）

    def provider(self) -> Provider:
        if self.provider_kind == "ollama":
            return Provider("ollama-local", self.base_url)
        return Provider("platform-model", self.base_url, self.api_key_env)


@dataclass
class ModelCall:
    """一次模型调用的过程记录，时间为毫秒时间戳。"""

    call: int
    start_ms: int
    first_delta_ms: Optional[int] = None
    end_ms: Optional[int] = None
    finish: Optional[str] = None
    error: Optional[str] = None
    usage: Optional[dict] = None
    phase: str = ""  # assessment / finalization / answer，由 Diagnoser 填写
    retries: int = 0  # 连接类错误后的重试次数（预算插件最多重试一次）
    model: str = ""  # 与对话模型不同时填写（例如看图模型）


@dataclass
class RunOutcome:
    answer: str
    finish_reason: Optional[str]
    usage: list = field(default_factory=list)
    error: Optional[str] = None  # MODEL_BUDGET_EXHAUSTED / CANCELLED / MODEL_STREAM_OUTPUT_TRUNCATED
    evidence: list = field(default_factory=list)
    tool_trace: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    partial: bool = False
    calls: list = field(default_factory=list)  # ModelCall；没有 end_ms 的调用即“未正常结束”
    tool_events: list = field(default_factory=list)  # {name, t, data}：tool_started / tool_finished
    budget_stop: Optional[str] = None  # 预算插件的停止原因，如 model_calls（检查次数用完）


class _StreamTail(threading.Thread):
    """读取插件和工具桥逐行追加的事件文件；只接受完整的 JSON 行。收到取消信号时写取消文件。"""

    def __init__(self, path: Path, cancel_path: Path, on_event: EventCallback, cancel: Optional[threading.Event]):
        super().__init__(daemon=True)
        self.path, self.cancel_path, self.on_event, self.cancel = path, cancel_path, on_event, cancel
        self.calls: dict = {}
        self.tool_events: list = []
        self.evidence: list = []
        self.traces: list = []
        self.limitations: list = []
        self.partial = False
        self._offset = 0
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.wait(STREAM_POLL_SECONDS):
            if self.cancel is not None and self.cancel.is_set() and not self.cancel_path.exists():
                self.cancel_path.touch()
            self._drain()
        self._drain()

    def stop(self) -> None:
        self._done.set()
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
            self._handle_row(json.loads(line))

    def _handle_row(self, row: dict) -> None:  # 不能叫 _handle：Python 3.13 的 Thread 用了这个属性名
        call, t = row.get("call"), row.get("t")
        record = self.calls.get(call)
        if row.get("start") is True:
            self.calls[call] = ModelCall(call, t)
            self.on_event("start", {"call": call, "t": t})
        elif isinstance(row.get("delta"), str):
            if record is not None and record.first_delta_ms is None:
                record.first_delta_ms = t
            self.on_event("delta", {"call": call, "t": t, "text": row["delta"]})
        elif isinstance(row.get("block"), str):
            self.on_event("block", {"call": call, "t": t, "type": row["block"]})
        elif isinstance(row.get("usage"), dict):
            if record is not None:
                record.usage = row["usage"]
        elif isinstance(row.get("finish"), str):
            if record is not None:
                record.end_ms, record.finish = t, row["finish"]
            self.on_event("finish", {"call": call, "t": t, "kind": row["finish"]})
        elif isinstance(row.get("retry"), str):
            if record is not None:
                record.retries += 1
        elif isinstance(row.get("error"), str):
            if record is not None:
                record.end_ms, record.error = t, row["error"]
            self.on_event("error", {"call": call, "t": t, "code": row["error"]})
        elif isinstance(row.get("tool_event"), str):
            event = {"name": row["tool_event"], "t": t, "data": row.get("data") or {}}
            self.tool_events.append(event)
            self.on_event("tool_event", event)
        elif isinstance(row.get("evidence"), dict):
            self.evidence.append(row["evidence"])
        elif isinstance(row.get("trace"), dict):
            self.traces.append(row["trace"])
        elif isinstance(row.get("limitation"), str):
            self.limitations.append(row["limitation"])
        elif row.get("partial") is True:
            self.partial = True


def run_once(prompt, settings: DshSettings, on_event: EventCallback,
             cancel: Optional[threading.Event] = None, tools: Optional[ToolContext] = None) -> RunOutcome:
    """阻塞执行一次 DSH 对话，应在工作线程中调用。tools 为 None 时不提供任何工具。

    prompt 是字符串或 LatePrompt（见模块说明）。"""
    import time

    from deepseek_harness import DeepSeekHarness

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=settings.state_dir))
    try:
        provider = settings.provider()
        stream_path, cancel_path = run_dir / "stream.jsonl", run_dir / "cancel.request"
        plugins = [provider_plugin(provider, model=settings.model, context_window=settings.context_window,
                                   max_output_tokens=settings.max_output_tokens,
                                   reasoning_off=settings.reasoning_off)]
        allowed = []
        if tools is not None and tools.scope.allowed_tools:
            allowed = [provider_tool_name(t.value) for t in sorted(tools.scope.allowed_tools, key=lambda t: t.value)]
            context_path = run_dir / "tools.json"
            fd = os.open(context_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(tools.model_copy(update={"events_path": str(stream_path),
                                                      "prefetch_path": str(run_dir / "prefetch.json")}).model_dump_json())
            plugins.append(tool_plugin(context_path, tools.tool_timeout_seconds))
        plugins.insert(1, budget_plugin(provider_id=provider.id, model=settings.model,
                                        max_model_calls=settings.max_model_calls,
                                        max_output_tokens=settings.max_output_tokens, max_output_bytes=MAX_OUTPUT_BYTES,
                                        deadline_unix=time.time() + settings.total_timeout_seconds,
                                        allowed_tools=allowed, run_dir=run_dir,
                                        first_token_seconds=settings.first_token_seconds))
        patch = run_dir / "runtime.patch.yml"
        patch.write_text(render_patch(plugins), encoding="utf-8")
        hosts = {urlsplit(provider.base_url).hostname, "localhost", "127.0.0.1"}
        if tools is not None:
            hosts.add(urlsplit(tools.gateway_url).hostname)
        no_proxy = ",".join(sorted(h for h in hosts if h))
        # DSH_TELEMETRY_DISABLED：DSH 官方遥测会把会话发往 DeepSeek 的服务器，在这里硬性关闭。
        env = {**provider.environment(), "NO_PROXY": no_proxy, "no_proxy": no_proxy, "DSH_TELEMETRY_DISABLED": "1"}
        if settings.system_prompt:
            env["DSH_SYSTEM_PROMPT"] = settings.system_prompt
        tail = _StreamTail(stream_path, cancel_path, on_event, cancel)
        tail.start()
        answer, finish_reason, failure = "", None, None
        try:
            harness = DeepSeekHarness(
                dsh_home=str(run_dir / "home"),
                cwd=str(run_dir),
                profile="sdk-minimal",
                patches=(str(patch),),
                provider=provider.id,
                model=settings.model,
                env=env,
                reasoning_effort="off" if settings.reasoning_off else None,
                max_tokens=settings.max_output_tokens,
                request_timeout_seconds=settings.total_timeout_seconds,
            )
            try:
                text = _start_while_preparing(harness, prompt, run_dir)
                read_budget_stop(run_dir)  # 约束没有挂载就不发第一条消息
                if cancel is not None and cancel.is_set():  # 准备期间已取消（超时或断开）：不再发出提问
                    cancel_path.touch()
                else:
                    try:
                        result = harness.run(text, session_id="request-" + uuid4().hex)
                        answer, finish_reason = result.final_response, result.finish_reason
                    except Exception as exc:  # 预算插件或取消中断时，DSH 可能以异常结束本轮
                        failure = exc
            finally:
                harness.close()
        finally:
            tail.stop()
        calls = [tail.calls[k] for k in sorted(tail.calls)]
        finishes = [c.finish for c in calls if c.finish]
        budget_stop = read_budget_stop(run_dir)
        if cancel_path.exists():
            error = "CANCELLED"
        elif budget_stop:
            error = "MODEL_BUDGET_EXHAUSTED"
        elif failure is not None:
            raise failure
        elif finish_reason == "error" or (calls and calls[-1].error == "stream_error"):
            # 模型调用失败（例如已经开始输出后连接断开，或重试后仍没有任何输出）：DSH 以 error 结束本轮而不抛异常。
            # 必须报告为错误，否则半截文字会被当成模型的正常输出（服务器测试里表现为 MODEL_OUTPUT_UNSTRUCTURED）。
            error = "MODEL_STREAM_INCOMPLETE" if answer else "MODEL_PROVIDER_ERROR"
        elif finishes and finishes[-1] in TRUNCATED_FINISHES:
            error = "MODEL_STREAM_OUTPUT_TRUNCATED"
        else:
            error = None
        return RunOutcome(answer, finish_reason, [c.usage for c in calls if c.usage], error,
                          tail.evidence, tail.traces, tail.limitations, tail.partial, calls, tail.tool_events,
                          budget_stop)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _start_while_preparing(harness, prompt, run_dir: Path) -> str:
    """启动 DSH；提问是 LatePrompt 时同时在当前线程里准备，返回最终的用户消息。启动失败时抛出启动的异常。"""
    if not isinstance(prompt, LatePrompt):
        harness.start()
        return prompt
    failure: list = []

    def start() -> None:
        try:
            harness.start()
        except BaseException as exc:  # 交回调用线程抛出
            failure.append(exc)

    starter = threading.Thread(target=start, name="dsh-start", daemon=True)
    starter.start()
    try:
        text, prefetch = prompt.build()
        if prefetch is not None:
            _write_prefetch(run_dir / "prefetch.json", prefetch)
    finally:
        starter.join()
    if failure:
        raise failure[0]
    return text


def _write_prefetch(path: Path, prefetch: Prefetch) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(prefetch.model_dump_json())
