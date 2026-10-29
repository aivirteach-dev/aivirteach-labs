from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

from .base import (
    ProviderMessage,
    ProviderStreamDone,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTool,
    ProviderTurn,
)


_FAKE_ANSWER = (
    "Agent 服务当前使用 fake 模型供应商，因此没有进行模型推理。"
    "配置 OpenAI-compatible 供应商后可执行完整诊断。"
)


class FakeProvider:
    """Deterministic provider for smoke tests and local development."""

    def __init__(self, turns: Iterable[ProviderTurn] | None = None) -> None:
        self._turns = deque(turns or [])

    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> ProviderTurn:
        if self._turns:
            return self._turns.popleft()
        draft = {
            "diagnosis": {
                "summary": "未配置真实模型供应商。",
                "probable_causes": [],
                "confidence": "low",
            },
            "course_alignment": {"expected": [], "observed": []},
            "evidence_ids": [],
            "suggested_actions": [],
            "limitations": ["FAKE_MODEL_PROVIDER"],
        }
        return ProviderTurn(text=json.dumps(draft, ensure_ascii=False))

    async def stream_complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> AsyncIterator[ProviderStreamEvent]:
        del tools
        answer = self._render_answer(messages)
        # Fixed-size chunks make local streaming deterministic while still
        # exercising incremental rendering instead of one buffered response.
        for offset in range(0, len(answer), 6):
            yield ProviderTextDelta(text=answer[offset : offset + 6])
        yield ProviderStreamDone(finish_reason="stop")

    @staticmethod
    def _render_answer(messages: Sequence[ProviderMessage]) -> str:
        for message in reversed(messages):
            if message.role != "user" or not message.content:
                continue
            try:
                context = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if not isinstance(context, dict):
                continue
            draft = context.get("validated_assessment")
            if not isinstance(draft, dict):
                continue
            answer = draft.get("diagnosis", {}).get("summary")
            if isinstance(answer, str) and answer:
                return answer
        return _FAKE_ANSWER

    async def aclose(self) -> None:
        return None
