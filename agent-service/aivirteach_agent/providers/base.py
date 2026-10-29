from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import Usage


class ProviderError(RuntimeError):
    """A normalized model-provider failure."""


@dataclass(frozen=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProviderMessage:
    role: str
    content: str | None = None
    tool_calls: tuple[ProviderToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ProviderTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ProviderTurn:
    text: str | None = None
    tool_calls: tuple[ProviderToolCall, ...] = field(default_factory=tuple)
    finish_reason: str = "stop"
    usage: Usage | None = None


@dataclass(frozen=True)
class ProviderTextDelta:
    """A user-visible text fragment from a streaming model response."""

    text: str


@dataclass(frozen=True)
class ProviderStreamDone:
    """The terminal event for a streaming model response."""

    finish_reason: str = "stop"
    usage: Usage | None = None


ProviderStreamEvent = ProviderTextDelta | ProviderStreamDone


class ModelProvider(Protocol):
    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> ProviderTurn: ...

    def stream_complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> AsyncIterator[ProviderStreamEvent]: ...

    async def aclose(self) -> None: ...
