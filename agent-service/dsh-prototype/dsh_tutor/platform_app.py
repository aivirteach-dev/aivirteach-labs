"""与 main 上现有 Agent 相同的平台接口，内部由 DSH 运行；评估 → 校验 → 回答的流程见 platform_assessment.py。

接口、请求/返回格式、鉴权、SSE 事件名与帧格式、环境变量均以 agent-service/aivirteach_agent
为准，Server 和 Client 不需要任何改动。启动见 start_platform.sh。
唯一的扩展是可选的虚拟机画面（请求 screen / 返回 screen_findings，见 platform_screen.py）；不带时与 main 相同。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .platform_course import CourseRepository
from .platform_mcp import ToolContext
from .platform_assessment import (MAX_ANSWER_CHARS, answer_prompt, assessment_fallback, assessment_prompt,
                                  finalization_prompt, is_answer_stage, validate)
from .platform_models import (CourseAlignment, DiagnoseRequest, Diagnosis, DiagnosticAssessment,
                              Evidence, ToolTrace, Usage)
from .platform_runtime import DshSettings, RunOutcome, run_once
from .platform_screen import (SCREEN_ANALYZED, SCREEN_ANALYZING, SCREEN_NOT_ANALYZED, BodySizeLimit,
                              ScreenDiagnoseRequest, ScreenDiagnoseResponse, ScreenFindings, ScreenRejected,
                              load_screen, validation_error_handler)
from .platform_vision import (SCREEN_ANALYSIS_FAILED, SCREEN_ANALYSIS_TIMEOUT, Perception, ScreenAnalyzer,
                              VisionSettings)
from .telemetry import RequestTrace, Telemetry, provider_name

LOG = logging.getLogger("aivirteach.dsh_agent")
SSE_HEARTBEAT_SECONDS = 10
API_KEY_ENV = "AIVIRTEACH_MODEL_API_KEY"
VISION_API_KEY_ENV = "AIVIRTEACH_VISION_MODEL_API_KEY"
# 看图之后至少给评估和回答留这么多秒；剩余时间不够就不看图（SCREEN_ANALYSIS_TIMEOUT）。
TIME_AFTER_SCREEN_SECONDS = 20
MIN_SCREEN_BUDGET_SECONDS = 3
# 与 main 的 DEFAULT_COURSE_DIRECTORY 相同（agent-service/.cache/...）。
DEFAULT_COURSE_DIRECTORY = Path(__file__).resolve().parents[2] / ".cache" / "course" / "AI Daily Briefing" / "processed"

Publish = Callable[[str, dict], Awaitable[None]]
Runner = Callable[..., RunOutcome]

agent_bearer = HTTPBearer(auto_error=False, scheme_name="AgentBearer",
                          description="AIVIRTEACH_AGENT_TOKEN — server-to-Agent diagnosis requests.")


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, low), high)


def normalize_base_url(raw: str) -> str:
    """main 只要求根地址（如 https://api.deepseek.com/）；DSH 要求以 /v1 结尾，无路径时补上。"""
    value = raw.strip().rstrip("/")
    if value and urlsplit(value).path == "":
        return value + "/v1"
    return value


@dataclass(frozen=True)
class Settings:
    agent_token: str = field(repr=False)
    model_provider: str
    model_base_url: str
    model_name: str
    model_api_key_set: bool
    model_thinking: str
    total_timeout_seconds: int
    max_concurrent_requests: int
    context_window: int
    max_output_tokens: int
    state_dir: Path
    course_directory: Path = DEFAULT_COURSE_DIRECTORY
    gateway_url: str = "http://127.0.0.1:8765"
    diagnostic_token: str = field(default="", repr=False)
    tool_timeout_seconds: int = 8
    max_reasoning_turns: int = 4
    max_tool_calls: int = 6
    max_tool_output_chars: int = 32_768
    # 看图模型（可选，与对话模型分开配置；模型名为空表示不看图）
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key_env: Optional[str] = None
    vision_timeout_seconds: int = 12
    screen_max_per_hour: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = normalize_base_url(os.getenv("AIVIRTEACH_MODEL_BASE_URL", ""))
        vision_key_env = (VISION_API_KEY_ENV if os.getenv(VISION_API_KEY_ENV)
                          else API_KEY_ENV if os.getenv(API_KEY_ENV) else None)
        return cls(
            agent_token=os.getenv("AIVIRTEACH_AGENT_TOKEN", ""),
            model_provider=os.getenv("AIVIRTEACH_MODEL_PROVIDER", "fake").strip().lower(),
            model_base_url=base_url,
            model_name=os.getenv("AIVIRTEACH_MODEL_NAME", ""),
            model_api_key_set=bool(os.getenv(API_KEY_ENV)),
            model_thinking=os.getenv("AIVIRTEACH_MODEL_THINKING", "").strip().lower(),
            total_timeout_seconds=_int_env("AIVIRTEACH_AGENT_TOTAL_TIMEOUT", 40, 5, 120),
            max_concurrent_requests=_int_env("AIVIRTEACH_MAX_CONCURRENT_REQUESTS", 4, 1, 32),
            context_window=_int_env("AIVIRTEACH_DSH_CONTEXT_WINDOW", 32_768, 8_192, 262_144),
            max_output_tokens=_int_env("AIVIRTEACH_DSH_MAX_OUTPUT_TOKENS", 4_096, 256, 8_192),
            state_dir=Path(os.getenv("AIVIRTEACH_DSH_STATE_DIR", str(Path(tempfile.gettempdir()) / "aivirteach-dsh"))),
            course_directory=Path(os.getenv("AIVIRTEACH_COURSE_DIR", str(DEFAULT_COURSE_DIRECTORY))),
            # 以下变量名、默认值和范围与 main 的 config.py 相同。
            gateway_url=os.getenv("AIVIRTEACH_GATEWAY_URL", "http://127.0.0.1:8765").rstrip("/"),
            diagnostic_token=os.getenv("AIVIRTEACH_DIAGNOSTIC_TOKEN", ""),
            tool_timeout_seconds=_int_env("AIVIRTEACH_TOOL_TIMEOUT", 8, 1, 30),
            max_reasoning_turns=_int_env("AIVIRTEACH_MAX_REASONING_TURNS", 4, 1, 8),
            max_tool_calls=_int_env("AIVIRTEACH_MAX_TOOL_CALLS", 6, 1, 12),
            max_tool_output_chars=_int_env("AIVIRTEACH_MAX_TOOL_OUTPUT_CHARS", 32_768, 1_024, 65_536),
            # 看图：地址和密钥默认沿用对话模型的配置。
            vision_model=os.getenv("AIVIRTEACH_VISION_MODEL_NAME", "").strip(),
            vision_base_url=normalize_base_url(os.getenv("AIVIRTEACH_VISION_MODEL_BASE_URL", "")) or base_url,
            vision_api_key_env=vision_key_env,
            vision_timeout_seconds=_int_env("AIVIRTEACH_VISION_TIMEOUT", 12, 3, 30),
            screen_max_per_hour=_int_env("AIVIRTEACH_SCREEN_MAX_PER_HOUR", 10, 0, 120),
        )

    def vision(self) -> VisionSettings:
        valid = urlsplit(self.vision_base_url).scheme in ("http", "https") and \
            urlsplit(self.vision_base_url).path == "/v1"
        return VisionSettings(model=self.vision_model if valid else "", base_url=self.vision_base_url,
                              api_key_env=self.vision_api_key_env, timeout_seconds=self.vision_timeout_seconds,
                              max_per_hour=self.screen_max_per_hour)

    def readiness_errors(self) -> list:
        errors = []
        if not self.agent_token:
            errors.append("AIVIRTEACH_AGENT_TOKEN is not configured")
        if not self.diagnostic_token:
            errors.append("AIVIRTEACH_DIAGNOSTIC_TOKEN is not configured")
        if urlsplit(self.gateway_url).scheme not in ("http", "https"):
            errors.append("AIVIRTEACH_GATEWAY_URL must be an http(s) URL")
        if self.model_provider not in ("fake", "ollama", "openai_compatible"):
            errors.append("AIVIRTEACH_MODEL_PROVIDER is unsupported")
        if self.model_provider in ("ollama", "openai_compatible"):
            if urlsplit(self.model_base_url).path != "/v1":
                errors.append("AIVIRTEACH_MODEL_BASE_URL must be a root URL or end with /v1")
            if not self.model_name:
                errors.append("AIVIRTEACH_MODEL_NAME is not configured")
        if self.model_provider == "openai_compatible" and not self.model_api_key_set:
            errors.append("AIVIRTEACH_MODEL_API_KEY is not configured")
        if self.model_thinking not in ("", "enabled", "disabled"):
            errors.append("AIVIRTEACH_MODEL_THINKING must be enabled or disabled")
        return errors

    def dsh(self, timeout_seconds: int, system_prompt: str = "", max_model_calls: Optional[int] = None) -> DshSettings:
        """一个阶段的 DSH 配置。评估阶段最多 max_reasoning_turns 次模型调用（与 main 相同），收尾和回答各 1 次。"""
        return DshSettings(
            provider_kind=self.model_provider,
            base_url=self.model_base_url,
            model=self.model_name,
            api_key_env=API_KEY_ENV if self.model_provider == "openai_compatible" else None,
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
            total_timeout_seconds=timeout_seconds,
            state_dir=self.state_dir,
            # 未设置时也关闭思考：课程问答不需要，且思考内容会占用输出预算。
            reasoning_off=self.model_thinking != "enabled",
            max_model_calls=max_model_calls or self.max_reasoning_turns,
            system_prompt=system_prompt,
        )

    def tools(self, request: DiagnoseRequest, timeout_seconds: int) -> Optional[ToolContext]:
        if not request.diagnostic_scope.allowed_tools:
            return None
        return ToolContext(lab_id=request.lab_id, scope=request.diagnostic_scope, gateway_url=self.gateway_url,
                           gateway_token=self.diagnostic_token, events_path="",
                           deadline_unix=time.time() + timeout_seconds,
                           tool_timeout_seconds=self.tool_timeout_seconds, max_tool_calls=self.max_tool_calls,
                           max_tool_output_chars=self.max_tool_output_chars)


def fake_runner(prompt: str, dsh: DshSettings, on_event: Callable[[str, dict], None],
                cancel: Optional[threading.Event] = None, tools: Optional[ToolContext] = None) -> RunOutcome:
    """与 main 的 fake 提供方用途相同：只验证接口联调，不做任何推理，也不调用工具。"""
    chinese = "用 zh" in dsh.system_prompt.lower()
    if not is_answer_stage(dsh.system_prompt):
        summary = ("fake 模型的固定评估，仅用于接口联调。" if chinese
                   else "Fixed assessment from the fake model for integration testing.")
        text = json.dumps({"diagnosis": {"summary": summary, "probable_causes": [], "confidence": "low"},
                           "course_alignment": {"expected": [], "observed": []}, "evidence_ids": [],
                           "suggested_actions": [], "limitations": []}, ensure_ascii=False)
        on_event("finish", {"call": 1, "kind": "stop"})
        return RunOutcome(text, "completed", [])
    text = ("这是 fake 模型的固定回复，仅用于接口联调，未进行任何推理。" if chinese
            else "This is a fixed reply from the fake model for integration testing; no reasoning was performed.")
    for index in range(0, len(text), 8):
        on_event("delta", {"call": 1, "text": text[index:index + 8]})
        time.sleep(0.01)
    on_event("finish", {"call": 1, "kind": "stop"})
    return RunOutcome(text, "completed", [])


def normalize_usage(chunks: list, provider_kind: str = "openai_compatible") -> Optional[Usage]:
    """把 DSH（pi-ai）的用量换成 main 的三项计量；对不上就返回 None，绝不猜测拆分。

    实测 DeepSeek 经 DSH：inputTokens 是未命中缓存的输入，有命中时另给 cacheReadTokens，
    且 inputTokens + cacheReadTokens + outputTokens == totalTokens。Ollama 不按缓存计量，与 main 一样不上报。
    """
    if not chunks or provider_kind != "openai_compatible":
        return None
    hit = miss = out = 0
    for raw in chunks:
        values = raw.get("inputTokens"), raw.get("cacheReadTokens", 0), raw.get("outputTokens"), raw.get("totalTokens")
        valid = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values)
        if not valid or raw.get("cacheWriteTokens") or values[0] + values[1] + values[2] != values[3]:
            LOG.warning("DSH usage does not add up; reporting null (keys=%s)", sorted(raw))
            return None
        miss, hit, out = miss + values[0], hit + values[1], out + values[2]
    return Usage(input_cache_hit_tokens=hit, input_cache_miss_tokens=miss, output_tokens=out)


def _unique(items: list) -> list:
    return list(dict.fromkeys(items))


@dataclass
class PipelineOutcome:
    """一次请求各阶段（评估、收尾、回答）的结果；calls / tool_events 给监控用。"""

    assessment: Optional[DiagnosticAssessment] = None
    evidence: list = field(default_factory=list)
    tool_trace: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    partial: bool = False
    answer: str = ""
    answer_error: Optional[str] = None  # 回答阶段的失败码
    answer_finish: Optional[str] = None
    usage: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    tool_events: list = field(default_factory=list)
    screen_findings: Optional[ScreenFindings] = None  # 返回给 Server 的看图结果
    perception: Optional[Perception] = None  # 完整的看图结果（窗口、页面、报错），后续阶段交给评估使用


class Diagnoser:
    """一次请求：排队 → 评估（DSH + 工具，不显示）→ 校验 → 回答（DSH，逐段推送）→ 组装 DiagnoseResponse。"""

    def __init__(self, settings: Settings, runner: Runner, courses: CourseRepository,
                 telemetry: Optional[Telemetry] = None, screen_analyzer: Optional[ScreenAnalyzer] = None):
        self.settings, self.runner, self.courses = settings, runner, courses
        self.telemetry = telemetry or Telemetry()
        # 进程内共用一个：同一张图的结果复用、每台 VM 的看图次数限制都在这里。
        self.screen_analyzer = screen_analyzer or ScreenAnalyzer(settings.vision())
        self.slots = asyncio.Semaphore(settings.max_concurrent_requests)

    async def diagnose(self, request: ScreenDiagnoseRequest,
                       publish: Optional[Publish] = None) -> ScreenDiagnoseResponse:
        trace, response = RequestTrace(), None
        try:
            response = await self._diagnose(request, publish, trace)
            return response
        finally:  # 调用方断开时 response 为 None，监控记为 CLIENT_DISCONNECTED
            self.telemetry.record(request, response, trace, stream=publish is not None)

    def _pipeline(self, request: ScreenDiagnoseRequest, deadline: float, on_event: Callable[[str, dict], None],
                  cancel: threading.Event) -> PipelineOutcome:
        """在工作线程里依次运行各阶段；阶段事件经 on_event("stage", ...) 交给 SSE。各阶段共用总时限。"""
        result = PipelineOutcome()

        def stage(name: str, data: Optional[dict] = None) -> None:
            on_event("stage", {"name": name, "data": data or {}})

        def run(phase: str, prompts: tuple, tools: Optional[ToolContext], max_model_calls: Optional[int]) -> RunOutcome:
            remaining = max(1, int(deadline - time.monotonic()))
            outcome = self.runner(prompts[1], self.settings.dsh(remaining, prompts[0], max_model_calls),
                                  on_event, cancel, tools)
            for call in outcome.calls:
                call.phase = phase
            result.calls.extend(outcome.calls)
            result.usage.extend(outcome.usage)
            result.tool_events.extend(outcome.tool_events)
            return outcome

        # 0. 虚拟机画面：检查截图，再交给看图模型；失败不影响后面的诊断和回答。
        screen_limitations = self._screen(request, stage, deadline, cancel, result)

        # 1. 评估：可以调工具，最后只输出评估 JSON；文字不推送给学生。
        stage("reasoning_started", {"turn": 1})
        text, limitations, partial, budget_stop = None, screen_limitations, False, None
        try:
            assessed = run("assessment", assessment_prompt(request),
                           self.settings.tools(request, max(1, int(deadline - time.monotonic()))), None)
            result.evidence, result.tool_trace = list(assessed.evidence), list(assessed.tool_trace)
            limitations.extend(assessed.limitations)
            partial, budget_stop = assessed.partial, assessed.budget_stop
            if assessed.error and budget_stop != "model_calls":
                partial = True
                limitations.append(assessed.error)
            elif not assessed.error:
                text = assessed.answer
        except Exception:
            LOG.exception("assessment run failed for request %s", request.request_id)
            partial = True
            limitations.append("MODEL_PROVIDER_ERROR")
        tool_calls = sum(1 for event in result.tool_events if event.get("name") == "tool_started")
        stage("reasoning_finished", {"turn": 1, "outcome": "answer_ready", "tool_call_count": tool_calls})
        if cancel.is_set():
            return result

        # 2. 收尾：检查次数用完还没给出评估，不带工具再问一次（main 的 FINALIZATION_PROMPT）。
        if budget_stop == "model_calls":
            partial = True
            limitations.append("REASONING_TURN_BUDGET_EXHAUSTED")
            stage("finalization_started", {"reason": "reasoning_turn_budget_exhausted"})
            try:
                final = run("finalization", finalization_prompt(request, result.evidence), None, 1)
                if final.error:
                    limitations.append(final.error)
                else:
                    text = final.answer
            except Exception:
                LOG.exception("finalization run failed for request %s", request.request_id)
                limitations.append("MODEL_PROVIDER_ERROR")
            if cancel.is_set():
                return result

        # 3. 校验：结构不对或引用了不存在的证据就不写回答。
        stage("assessment_validating")
        assessment, evidence, limitations, partial = validate(text, result.evidence, limitations, partial)
        result.assessment, result.evidence, result.limitations, result.partial = assessment, evidence, limitations, partial
        if assessment is None:
            stage("answer_failed", {"code": "ASSESSMENT_INVALID"})
            return result
        stage("assessment_validated", {"status": "partial" if partial else "completed"})

        # 4. 回答：不带工具，只根据校验过的评估写，逐段推送。
        stage("answer_started", {"phase": "final_answer"})
        try:
            answered = run("answer", answer_prompt(request, assessment, evidence, limitations), None, 1)
            result.answer, result.answer_finish, result.answer_error = answered.answer or "", answered.finish_reason, answered.error
        except Exception:
            LOG.exception("answer run failed for request %s", request.request_id)
            result.answer_error = "MODEL_STREAM_ERROR"
        return result

    def _screen(self, request: ScreenDiagnoseRequest, stage: Callable[..., None], deadline: float,
                cancel: threading.Event, result: PipelineOutcome) -> list:
        """检查并看图，返回要记进 limitations 的原因码；看图结果和用量写进 result。"""
        capture = getattr(request, "screen", None)
        if capture is None:
            return []
        try:
            loaded = load_screen(capture)
        except ScreenRejected as rejected:
            stage(SCREEN_ANALYZED, {"status": "rejected", "code": rejected.code})
            return [rejected.code]
        try:
            if not self.screen_analyzer.enabled:
                stage(SCREEN_ANALYZED, {"status": "skipped", "code": SCREEN_NOT_ANALYZED})
                return [SCREEN_NOT_ANALYZED]
            budget = min(self.settings.vision_timeout_seconds,
                         deadline - time.monotonic() - TIME_AFTER_SCREEN_SECONDS)
            if budget < MIN_SCREEN_BUDGET_SECONDS:
                stage(SCREEN_ANALYZED, {"status": "failed", "code": SCREEN_ANALYSIS_TIMEOUT})
                return [SCREEN_ANALYSIS_TIMEOUT]
            stage(SCREEN_ANALYZING)
            outcome = self.screen_analyzer.analyze(
                loaded, question=request.question, lesson_title=request.current_step.title, lab_id=request.lab_id,
                language=request.response_language, budget_seconds=budget, cancel=cancel)
        except Exception:  # 看图出任何意外都不能影响诊断
            LOG.exception("screen analysis failed for request %s", request.request_id)
            stage(SCREEN_ANALYZED, {"status": "failed", "code": SCREEN_ANALYSIS_FAILED})
            return [SCREEN_ANALYSIS_FAILED]
        finally:
            loaded.close()
        result.calls.extend(outcome.calls)
        result.usage.extend(outcome.usage)
        result.screen_findings = outcome.findings
        result.perception = outcome.perception if outcome.status == "ok" else None
        data = {"status": outcome.status, "passes": outcome.passes, "cached": outcome.cached}
        if outcome.code:
            data["code"] = outcome.code
        stage(SCREEN_ANALYZED, data)
        return [outcome.code] if outcome.code else []

    async def _diagnose(self, request: ScreenDiagnoseRequest, publish: Optional[Publish],
                        trace: RequestTrace) -> ScreenDiagnoseResponse:
        async def emit(event: str, data: dict) -> None:
            if publish is not None:
                await publish(event, data)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.total_timeout_seconds
        request = self.courses.enrich(request)
        limitations = [] if request.diagnostic_scope.allowed_tools else ["LIVE_DIAGNOSTICS_NOT_RUN"]
        await emit("context_ready", {"course_id": request.course.course_id, "lesson_id": request.current_step.lesson_id})
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=self.settings.total_timeout_seconds)
        except asyncio.TimeoutError:
            await emit("answer_failed", {"code": "AGENT_BUSY"})
            return self._response(request, None, [*limitations, "AGENT_BUSY"], None)

        trace.queue_wait_ms = (time.time_ns() - trace.started_ns) // 1_000_000
        cancel = threading.Event()
        events: asyncio.Queue = asyncio.Queue()
        on_event = lambda kind, data: loop.call_soon_threadsafe(events.put_nowait, (kind, data))
        # loop.time() 与 time.monotonic() 是同一个时钟；排队用掉的时间也从各阶段的预算里扣掉。
        task = asyncio.ensure_future(asyncio.to_thread(self._pipeline, request, deadline, on_event, cancel))
        # 名额等工作线程真正结束才归还，避免实际并发超过上限。
        task.add_done_callback(lambda _: self.slots.release())
        trace.task = task

        streamed: list = []
        answering = False

        async def handle(kind: str, data: dict) -> None:
            nonlocal answering
            if kind == "stage":
                answering = answering or data["name"] == "answer_started"
                await emit(data["name"], data["data"])
            elif kind == "delta" and answering:  # 评估阶段的文字是 JSON，不推送
                remaining = MAX_ANSWER_CHARS - sum(len(piece) for piece in streamed)
                if remaining > 0 and data["text"]:
                    streamed.append(data["text"][:remaining])
                    await emit("assistant_delta", {"delta": streamed[-1]})
            elif kind == "tool_event" and data["name"] in ("tool_started", "tool_finished"):
                await emit(data["name"], data["data"])

        try:
            while True:
                getter = asyncio.ensure_future(events.get())
                done, _ = await asyncio.wait({getter, task}, timeout=max(0.0, deadline - loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    await handle(*getter.result())
                    continue
                getter.cancel()
                if task in done:
                    while not events.empty():  # 线程结束前已入队的事件也要处理
                        await handle(*events.get_nowait())
                    break
                cancel.set()
                await emit("answer_failed", {"code": "AGENT_TOTAL_TIMEOUT"})
                return self._response(request, None, [*limitations, "AGENT_TOTAL_TIMEOUT"], None)
        except asyncio.CancelledError:
            cancel.set()  # 调用方断开：通知 DSH 停止生成
            raise

        try:
            outcome: PipelineOutcome = task.result()
        except Exception:
            LOG.exception("DSH pipeline failed for request %s", request.request_id)
            await emit("answer_failed", {"code": "MODEL_PROVIDER_ERROR"})
            return self._response(request, None, [*limitations, "MODEL_PROVIDER_ERROR"], None)

        limitations = _unique([*limitations, *outcome.limitations])
        usage = normalize_usage(outcome.usage, self.settings.model_provider)
        assessment = outcome.assessment
        details = {"evidence": outcome.evidence, "tool_trace": outcome.tool_trace, "assessment": assessment,
                   "screen_findings": outcome.screen_findings}
        if assessment is None:  # ASSESSMENT_INVALID 已经推送；与 main 一样只返回兜底文字
            return self._response(request, None, limitations, usage, **details)
        answer = (outcome.answer or "".join(streamed)).strip()
        error = outcome.answer_error
        if len(answer) > MAX_ANSWER_CHARS:
            answer, error = answer[:MAX_ANSWER_CHARS], error or "MODEL_STREAM_OUTPUT_TRUNCATED"
        if error == "MODEL_STREAM_OUTPUT_TRUNCATED" and answer:
            # 学生已经看到了大部分内容：保留截断的回答并标记 partial，而不是换成兜底文案。
            await emit("answer_finished", {"status": "truncated", "character_count": len(answer),
                                           "finish_reason": "length"})
            return self._response(request, answer, [*limitations, error], usage, **details)
        failure = error or (None if answer and outcome.answer_finish == "completed"
                            else "MODEL_STREAM_EMPTY" if not answer else "MODEL_STREAM_INCOMPLETE")
        if failure:
            await emit("answer_failed", {"code": failure})
            await emit("answer_finished", {"status": "fallback", "character_count": len("".join(streamed)),
                                           "finish_reason": outcome.answer_finish or "unknown"})
            fallback = assessment_fallback(assessment, request.response_language)
            return self._response(request, fallback, [*limitations, failure], usage, **details)
        await emit("answer_finished", {"status": "completed", "character_count": len(answer), "finish_reason": "stop"})
        return self._response(request, answer, limitations, usage, completed=not outcome.partial, **details)

    @staticmethod
    def _response(request, answer, limitations, usage, completed=False, evidence=(), tool_trace=(),
                  assessment: Optional[DiagnosticAssessment] = None,
                  screen_findings: Optional[ScreenFindings] = None) -> ScreenDiagnoseResponse:
        return ScreenDiagnoseResponse(
            request_id=request.request_id,
            status="completed" if completed and assessment is not None else "partial",
            answer=answer or assessment_fallback(None, request.response_language),
            diagnosis=assessment.diagnosis if assessment else Diagnosis(),
            course_alignment=assessment.course_alignment if assessment else CourseAlignment(),
            evidence=[Evidence.model_validate(item) for item in evidence],
            suggested_actions=assessment.suggested_actions if assessment else [],
            tool_trace=[ToolTrace.model_validate(item) for item in tool_trace],
            limitations=_unique(limitations),
            usage=usage,
            screen_findings=screen_findings,
        )


def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def create_app(*, settings: Optional[Settings] = None, runner: Optional[Runner] = None,
               course_repository: Optional[CourseRepository] = None,
               telemetry: Optional[Telemetry] = None, screen_analyzer: Optional[ScreenAnalyzer] = None) -> FastAPI:
    config = settings or Settings.from_env()
    courses = course_repository or CourseRepository(config.course_directory)
    selected_runner = runner or (fake_runner if config.model_provider == "fake" else run_once)
    monitor = telemetry or Telemetry.from_env(provider_name=provider_name(config.model_provider, config.model_base_url),
                                              model=config.model_name or config.model_provider)
    diagnoser = Diagnoser(config, selected_runner, courses, monitor, screen_analyzer)
    if config.vision_model and not diagnoser.screen_analyzer.enabled:
        LOG.warning("AIVIRTEACH_VISION_MODEL_NAME is set but the vision base URL is not a root or /v1 URL; "
                    "screens will not be analyzed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        monitor.shutdown()  # 退出前发完缓冲的监控数据

    app = FastAPI(title="AIVirTeach Troubleshooting Agent (DSH)", version="0.3.0", lifespan=lifespan,
                  description="Same platform contract as aivirteach_agent; runs on DeepSeek Harness.")
    origins = os.getenv("AIVIRTEACH_AGENT_CORS_ORIGINS", "http://127.0.0.1:8780,http://localhost:8780")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip().rstrip("/") for o in origins.split(",") if o.strip()],
                       allow_credentials=False, allow_methods=["POST", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type"])
    app.add_middleware(BodySizeLimit)  # 带截图的请求约 4 MB；更大的请求体在解析前就拒绝
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    async def require_agent_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(agent_bearer)) -> None:
        if not config.agent_token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIVIRTEACH_AGENT_TOKEN is not configured.")
        if (credentials is None or credentials.scheme.lower() != "bearer"
                or not hmac.compare_digest(credentials.credentials, config.agent_token)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing bearer token.",
                                headers={"WWW-Authenticate": "Bearer"})

    def require_ready() -> None:
        if config.readiness_errors():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Agent dependencies are not configured.")

    @app.get("/health", tags=["service"])
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", tags=["service"])
    async def ready() -> dict:
        errors = config.readiness_errors()
        if errors:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"status": "not_ready", "errors": errors})
        return {"status": "ready", "provider": config.model_provider,
                "diagnostic_gateway_configured": True, "processed_courses": courses.course_count}

    @app.post("/v1/agent/diagnose", response_model=ScreenDiagnoseResponse,
              dependencies=[Depends(require_agent_token)], tags=["agent"])
    async def diagnose(request: ScreenDiagnoseRequest) -> ScreenDiagnoseResponse:
        require_ready()
        return await diagnoser.diagnose(request)

    @app.post("/v1/agent/diagnose/stream", response_class=StreamingResponse,
              dependencies=[Depends(require_agent_token)], tags=["agent"])
    async def diagnose_stream(request: ScreenDiagnoseRequest) -> StreamingResponse:
        require_ready()
        request_id = str(request.request_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        async def publish(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def produce() -> None:
            cancelled = False
            try:
                await publish("started", {"phase": "diagnosis"})
                result = await diagnoser.diagnose(request, publish)
                await publish("result", {"response": result.model_dump(mode="json")})
                await publish("done", {"status": result.status})
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception:
                LOG.exception("diagnosis stream failed for request %s", request_id)
                await publish("error", {"code": "AGENT_STREAM_FAILED", "message": "The diagnosis stream ended unexpectedly."})
            finally:
                # 取消后已无人读取，队列满时不能让生产者卡在这里。
                if not cancelled:
                    await queue.put(None)

        async def events() -> AsyncIterator[str]:
            sequence = 0
            producer = asyncio.ensure_future(produce())

            def encoded(event: str, data: dict) -> str:
                nonlocal sequence
                sequence += 1
                return _sse_event(event, {"schema_version": 1, "request_id": request_id, "sequence": sequence, **data})

            try:
                yield encoded("accepted", {"status": "accepted"})
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if item is None:
                        break
                    yield encoded(*item)
            finally:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    app.state.settings = config
    return app


app = create_app()
