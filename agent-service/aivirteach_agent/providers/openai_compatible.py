from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from ..models import Usage
from .base import (
    ProviderError,
    ProviderMessage,
    ProviderStreamDone,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTool,
    ProviderToolCall,
    ProviderTurn,
)

LOG = logging.getLogger("aivirteach.agent")


def normalize_usage(raw: Any) -> Usage | None:
    """Map a Chat Completions ``usage`` object to ``Usage``.

    Missing or malformed usage yields ``None`` (never raises) so a metering
    problem cannot fail a diagnosis. Without the cache hit/miss breakdown the
    usage is also ``None``: guessing a split would silently mis-price the turn.
    """

    usage = _usage_from_dict(raw) if isinstance(raw, dict) else None
    if usage is None:
        LOG.warning("model provider returned missing or malformed token usage")
    return usage


def _usage_from_dict(raw: dict[str, Any]) -> Usage | None:
    hit = raw.get("prompt_cache_hit_tokens")
    miss = raw.get("prompt_cache_miss_tokens")
    output = raw.get("completion_tokens")
    if not all(_is_token_count(value) for value in (hit, miss, output)):
        return None
    return Usage(
        input_cache_hit_tokens=hit,
        input_cache_miss_tokens=miss,
        output_tokens=output,
    )


def _is_token_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class OpenAICompatibleProvider:
    """Chat Completions compatible adapter without a vendor SDK dependency."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
        thinking: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._thinking = thinking
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> ProviderTurn:
        payload = self._request_payload(messages=messages, tools=tools)

        try:
            response = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"model provider request failed: {type(exc).__name__}") from exc

        try:
            choice = body["choices"][0]
            message = choice["message"]
            calls = tuple(self._parse_tool_call(item) for item in message.get("tool_calls", []))
            return ProviderTurn(
                text=message.get("content"),
                tool_calls=calls,
                finish_reason=choice.get("finish_reason", "unknown"),
                usage=normalize_usage(body.get("usage")),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("model provider returned an invalid response") from exc

    async def stream_complete(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Yield only learner-visible text from a Chat Completions SSE stream.

        Reasoning content and streamed tool-call arguments are deliberately not
        surfaced. Tool-assisted reasoning continues to use ``complete``; this
        stream is intended for rendering an already validated final answer.
        """

        payload = self._request_payload(messages=messages, tools=tools)
        payload["stream"] = True
        # Without this the provider never sends token usage while streaming.
        payload["stream_options"] = {"include_usage": True}
        finish_reason: str | None = None
        raw_usage: Any = None
        stream_finished = False

        try:
            async with self._client.stream(
                "POST",
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "text/event-stream",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith(":"):
                        continue
                    if not stripped.startswith("data:"):
                        continue

                    raw_event = stripped[5:].strip()
                    if raw_event == "[DONE]":
                        stream_finished = True
                        break
                    if not raw_event:
                        continue

                    try:
                        event = json.loads(raw_event)
                    except (TypeError, ValueError) as exc:
                        raise ProviderError(
                            "model provider returned an invalid streaming response"
                        ) from exc
                    if not isinstance(event, dict):
                        raise ProviderError(
                            "model provider returned an invalid streaming response"
                        )
                    if event.get("error") is not None:
                        raise ProviderError("model provider stream returned an error")

                    # The usage-only final event has no choices, so capture
                    # usage before the empty-choices skip below.
                    if event.get("usage") is not None:
                        raw_usage = event["usage"]

                    choices = event.get("choices")
                    # OpenAI-compatible providers may send a final usage-only
                    # event with an empty choices array.
                    if choices == []:
                        continue
                    if not isinstance(choices, list) or not choices:
                        raise ProviderError(
                            "model provider returned an invalid streaming response"
                        )

                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise ProviderError(
                            "model provider returned an invalid streaming response"
                        )
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        raise ProviderError(
                            "model provider returned an invalid streaming response"
                        )

                    # Never expose chain-of-thought fields such as
                    # ``reasoning_content``. Only ordinary answer content is
                    # allowed through the provider boundary.
                    content = delta.get("content")
                    if content is not None and not isinstance(content, str):
                        raise ProviderError(
                            "model provider returned an invalid streaming response"
                        )
                    if content:
                        yield ProviderTextDelta(text=content)

                    raw_finish_reason = choice.get("finish_reason")
                    if raw_finish_reason is not None:
                        if not isinstance(raw_finish_reason, str):
                            raise ProviderError(
                                "model provider returned an invalid streaming response"
                            )
                        finish_reason = raw_finish_reason
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"model provider request failed: {type(exc).__name__}"
            ) from exc

        # A few compatible providers close the response immediately after a
        # chunk containing finish_reason and omit the literal [DONE] sentinel.
        # Accept that shape, but reject a silently truncated stream.
        if not stream_finished and finish_reason is None:
            raise ProviderError("model provider stream ended before completion")
        yield ProviderStreamDone(
            finish_reason=finish_reason or "stop",
            usage=normalize_usage(raw_usage),
        )

    def _request_payload(
        self,
        *,
        messages: Sequence[ProviderMessage],
        tools: Sequence[ProviderTool],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message_payload(message) for message in messages],
            "temperature": 0.1,
        }
        if self._thinking:
            payload["thinking"] = {"type": self._thinking}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _message_payload(message: ProviderMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _parse_tool_call(item: dict[str, Any]) -> ProviderToolCall:
        function = item["function"]
        raw_arguments = function.get("arguments") or "{}"
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return ProviderToolCall(
            id=str(item["id"]),
            name=str(function["name"]),
            arguments=arguments,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
