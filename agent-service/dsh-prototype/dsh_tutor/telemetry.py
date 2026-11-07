"""监控：每个请求按 OpenTelemetry 发一条 trace，后端用 Phoenix（见 docs/monitoring.md）。

只在设置了 OTEL_EXPORTER_OTLP_ENDPOINT 或 OTEL_EXPORTER_OTLP_TRACES_ENDPOINT 时开启，否则什么都不做。
一条 trace 的结构：
  invoke_agent（AGENT，整个请求）
    ├─ chat <模型>（LLM，每次模型调用一个：耗时、首字延迟、token、结束原因）
    └─ execute_tool <工具>（TOOL，每次工具调用一个：耗时、结果状态、错误码）
时间取自事件文件里的毫秒时间戳，span 在请求结束后一次性补记，不拖慢回答；导出失败只记日志，不影响请求。
属性同时写 OpenTelemetry GenAI 约定（gen_ai.*，标准仍在演进）和 OpenInference 约定（Phoenix 直接识别，含缓存 token）。
默认不记录学生问题和回答原文；AIVIRTEACH_OTEL_CAPTURE_CONTENT=true 时才记录，只建议在测试环境打开。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

LOG = logging.getLogger("aivirteach.dsh_agent.telemetry")
SERVICE_NAME = "aivirteach-dsh-agent"
PROJECT_NAME = "aivirteach-agent"  # Phoenix 里的项目名，可用 OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=... 覆盖
AGENT_NAME = "aivirteach-tutor"
# 返回里出现这些限制码说明本次请求没有正常完成，span 标为 ERROR。
FAILURE_CODES = ("AGENT_BUSY", "AGENT_TOTAL_TIMEOUT", "MODEL_PROVIDER_ERROR", "MODEL_BUDGET_EXHAUSTED", "CANCELLED",
                 "MODEL_STREAM_INCOMPLETE", "MODEL_STREAM_OUTPUT_TRUNCATED", "MODEL_STREAM_EMPTY",
                 "MODEL_STREAM_ERROR", "MODEL_OUTPUT_UNSTRUCTURED", "MODEL_REFERENCED_UNKNOWN_EVIDENCE")
# 模型调用以这些结束原因收尾时视为失败（例如连不上模型服务时 DSH 给出 error）。
ERROR_FINISHES = ("error", "aborted")


def provider_name(model_provider: str, base_url: str) -> str:
    """gen_ai.provider.name / llm.provider 的取值。"""
    if model_provider == "ollama":
        return "ollama"
    host = urlsplit(base_url).hostname or ""
    return "deepseek" if host == "deepseek.com" or host.endswith(".deepseek.com") else model_provider


def tracing_requested() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


@dataclass
class RequestTrace:
    """Diagnoser 在一次请求中填写的监控信息。"""

    started_ns: int = field(default_factory=time.time_ns)
    queue_wait_ms: Optional[int] = None
    task: Any = None  # 运行 DSH 的 asyncio 任务；超时或断开时等它真正结束再补记子 span


def _ns(ms: Optional[int]) -> Optional[int]:
    return None if ms is None else int(ms) * 1_000_000


def _usage_attributes(usage: Optional[dict]) -> dict:
    """DSH 的 inputTokens 不含缓存命中；两套约定里的输入 token 都要求包含缓存部分。"""
    if not isinstance(usage, dict):
        return {}
    values = {k: usage.get(k) for k in ("inputTokens", "outputTokens", "cacheReadTokens")}
    if not all(isinstance(values[k], int) for k in ("inputTokens", "outputTokens")):
        return {}
    cache = values["cacheReadTokens"] if isinstance(values["cacheReadTokens"], int) else 0
    prompt, completion = values["inputTokens"] + cache, values["outputTokens"]
    return {
        "gen_ai.usage.input_tokens": prompt,
        "gen_ai.usage.output_tokens": completion,
        "gen_ai.usage.cache_read.input_tokens": cache,
        "llm.token_count.prompt": prompt,
        "llm.token_count.completion": completion,
        "llm.token_count.total": prompt + completion,
        "llm.token_count.prompt_details.cache_read": cache,
    }


class Telemetry:
    """tracer 为 None 时所有方法都是空操作。"""

    def __init__(self, tracer=None, provider=None, *, provider_name: str = "", model: str = "",
                 capture_content: bool = False) -> None:
        self.tracer, self._provider = tracer, provider
        self.provider_name, self.model, self.capture_content = provider_name, model, capture_content

    @classmethod
    def from_env(cls, *, provider_name: str, model: str) -> "Telemetry":
        if not tracing_requested():
            return cls()
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import OTELResourceDetector, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # 默认值在前，OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES 可以覆盖。
        env = OTELResourceDetector().detect().attributes
        resource = Resource.create({"service.name": SERVICE_NAME, "openinference.project.name": PROJECT_NAME, **env})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # 地址、请求头读 OTEL_EXPORTER_OTLP_*
        capture = os.getenv("AIVIRTEACH_OTEL_CAPTURE_CONTENT", "").strip().lower() == "true"
        LOG.info("OpenTelemetry tracing enabled (content capture %s)", "on" if capture else "off")
        return cls(provider.get_tracer("aivirteach.dsh_agent"), provider, provider_name=provider_name, model=model,
                   capture_content=capture)

    @property
    def enabled(self) -> bool:
        return self.tracer is not None

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()  # 退出前把缓冲里的 span 发完

    def record(self, request, response, trace: RequestTrace, *, stream: bool) -> None:
        """请求结束时调用。DSH 还在运行（超时、调用方断开）时等它结束再记，才能拿到每次调用的数据。"""
        if not self.enabled:
            return
        response_ns = time.time_ns()
        task = trace.task
        if task is not None and not task.done():
            task.add_done_callback(lambda done: self._safe_emit(request, response, trace, stream, response_ns, done))
        else:
            self._safe_emit(request, response, trace, stream, response_ns, task)

    def _safe_emit(self, request, response, trace, stream, response_ns, task) -> None:
        try:
            outcome = None
            if task is not None and not task.cancelled() and task.exception() is None:
                outcome = task.result()
            self._emit(request, response, trace, stream, response_ns, outcome)
        except Exception:  # 监控出错不能影响请求
            LOG.warning("telemetry span skipped for request %s", getattr(request, "request_id", "?"), exc_info=True)

    def _emit(self, request, response, trace, stream, response_ns, outcome) -> None:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import SpanKind, Status, StatusCode

        calls = list(getattr(outcome, "calls", []) or [])
        tools = [e for e in (getattr(outcome, "tool_events", []) or []) if e.get("name") == "tool_finished"]
        limitations = list(response.limitations) if response is not None else []
        failure = next((code for code in limitations if code in FAILURE_CODES), None)
        if response is None:
            failure = "CLIENT_DISCONNECTED"
        # 学生看到的回答来自最后一次模型调用（回答阶段）；评估阶段的文字不显示，不算首字。
        first_delta = calls[-1].first_delta_ms if calls else None
        ends = [response_ns] + [_ns(c.end_ms) for c in calls if c.end_ms] + [_ns(e.get("t")) for e in tools if e.get("t")]

        attributes = {
            "openinference.span.kind": "AGENT",
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": AGENT_NAME,
            "gen_ai.provider.name": self.provider_name,
            "gen_ai.request.model": self.model,
            "llm.provider": self.provider_name,
            "llm.model_name": self.model,
            "aivirteach.request_id": str(request.request_id),
            "aivirteach.lab_id": str(request.lab_id),
            "aivirteach.course_id": str(request.course.course_id),
            "aivirteach.lesson_id": str(request.current_step.lesson_id),
            "aivirteach.stream": stream,
            "aivirteach.status": response.status if response is not None else "cancelled",
            "aivirteach.limitations": limitations,
            "aivirteach.model_calls": len(calls),
            "aivirteach.tool_calls": len(tools),
            "aivirteach.response_ms": (response_ns - trace.started_ns) // 1_000_000,
        }
        if trace.queue_wait_ms is not None:
            attributes["aivirteach.queue_wait_ms"] = trace.queue_wait_ms
        if first_delta is not None:
            attributes["aivirteach.time_to_first_token_ms"] = max(0, first_delta - trace.started_ns // 1_000_000)
        if self.capture_content:
            attributes.update({"input.value": request.question, "input.mime_type": "text/plain"})
            if response is not None:
                attributes.update({"output.value": response.answer, "output.mime_type": "text/markdown"})

        root = self.tracer.start_span(f"invoke_agent {AGENT_NAME}", kind=SpanKind.INTERNAL,
                                      start_time=trace.started_ns, attributes=attributes)
        if failure:
            root.set_attribute("error.type", failure)
            root.set_status(Status(StatusCode.ERROR, failure))
        context = otel_trace.set_span_in_context(root)

        for call in calls:
            model = getattr(call, "model", "") or self.model  # 看图调用用的是单独配置的模型
            span = self.tracer.start_span(f"chat {model}", context=context, kind=SpanKind.CLIENT,
                                          start_time=_ns(call.start_ms), attributes={
                                              "openinference.span.kind": "LLM",
                                              "gen_ai.operation.name": "chat",
                                              "gen_ai.provider.name": self.provider_name,
                                              "gen_ai.request.model": model,
                                              "llm.provider": self.provider_name,
                                              "llm.model_name": model,
                                              "aivirteach.call": call.call,
                                              "aivirteach.phase": getattr(call, "phase", "") or "unknown",
                                              **_usage_attributes(call.usage)})
            if call.first_delta_ms is not None:
                span.set_attribute("aivirteach.time_to_first_token_ms", call.first_delta_ms - call.start_ms)
            if call.finish:
                span.set_attribute("gen_ai.response.finish_reasons", [call.finish])
            if getattr(call, "retries", 0):
                span.set_attribute("aivirteach.retries", call.retries)
            if call.error or call.end_ms is None or call.finish in ERROR_FINISHES:
                code = call.error or (call.finish if call.finish in ERROR_FINISHES else "not_finished")
                span.set_attribute("error.type", code)
                span.set_status(Status(StatusCode.ERROR, code))
            span.end(end_time=_ns(call.end_ms) or max(ends))

        for event in tools:
            data, end = event.get("data") or {}, _ns(event.get("t"))
            duration = data.get("duration_ms") if isinstance(data.get("duration_ms"), int) else 0
            tool, status = str(data.get("tool", "unknown")), str(data.get("status", "unknown"))
            span = self.tracer.start_span(f"execute_tool {tool}", context=context, kind=SpanKind.INTERNAL,
                                          start_time=end - duration * 1_000_000, attributes={
                                              "openinference.span.kind": "TOOL",
                                              "gen_ai.operation.name": "execute_tool",
                                              "gen_ai.tool.name": tool,
                                              "tool.name": tool,
                                              "aivirteach.tool_status": status})
            if data.get("observation_id"):
                span.set_attribute("aivirteach.observation_id", str(data["observation_id"]))
            if status in ("error", "denied"):
                code = str(data.get("error_code") or status)
                span.set_attribute("error.type", code)
                span.set_status(Status(StatusCode.ERROR, code))
            span.end(end_time=end)

        root.end(end_time=max(ends))
