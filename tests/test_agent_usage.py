import json
import unittest
from typing import Any

import httpx
from pydantic import ValidationError
from test_agent import FakeGateway, request_payload, settings

from aivirteach_agent.models import DiagnoseRequest, DiagnoseResponse, Usage
from aivirteach_agent.orchestrator import AgentOrchestrator
from aivirteach_agent.providers import (
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderMessage,
    ProviderStreamDone,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderTurn,
)
from aivirteach_agent.providers.base import ProviderError
from aivirteach_agent.providers.openai_compatible import normalize_usage

# Constructed from official docs (DeepSeek chat completion `usage` object),
# not a captured live response. prompt_tokens == cache hit + cache miss;
# reasoning_tokens is already included inside completion_tokens.
DEEPSEEK_USAGE: dict[str, Any] = {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "total_tokens": 1200,
    "prompt_cache_hit_tokens": 640,
    "prompt_cache_miss_tokens": 360,
    "completion_tokens_details": {"reasoning_tokens": 50},
}
DEEPSEEK_EXPECTED = Usage(
    input_cache_hit_tokens=640, input_cache_miss_tokens=360, output_tokens=200
)

FINAL_ASSESSMENT = json.dumps({"diagnosis": {"summary": "Docker 服务未运行。"}}, ensure_ascii=False)


def usage(hit: int, miss: int, out: int) -> Usage:
    return Usage(
        input_cache_hit_tokens=hit, input_cache_miss_tokens=miss, output_tokens=out
    )


class NormalizeUsageTests(unittest.TestCase):
    def test_cache_breakdown_is_mapped(self) -> None:
        self.assertEqual(normalize_usage(DEEPSEEK_USAGE), DEEPSEEK_EXPECTED)

    def test_without_cache_fields_is_none_not_a_guessed_split(self) -> None:
        # A provider without the cache breakdown must not be booked as all
        # miss (or all hit): that would silently mis-price every turn.
        raw = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
        with self.assertLogs("aivirteach.agent", level="WARNING"):
            self.assertIsNone(normalize_usage(raw))

    def test_zero_values_are_valid(self) -> None:
        raw = {
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }
        self.assertEqual(normalize_usage(raw), usage(0, 0, 0))

    def test_absent_or_malformed_usage_is_none_and_logs_warning(self) -> None:
        cases: list[Any] = [
            None,
            "usage",
            [],
            {},
            {"prompt_tokens": 10},  # no completion_tokens
            {"completion_tokens": 5},  # no prompt info at all
            {"prompt_tokens": -1, "completion_tokens": 5},
            {"prompt_tokens": 10, "completion_tokens": -5},
            {"prompt_tokens": "10", "completion_tokens": 5},
            {"prompt_tokens": 10.5, "completion_tokens": 5},
            {"prompt_tokens": True, "completion_tokens": 5},
            {**DEEPSEEK_USAGE, "prompt_cache_hit_tokens": -1},
            {**DEEPSEEK_USAGE, "prompt_cache_miss_tokens": "360"},
            # Only half of the cache breakdown is not trustworthy.
            {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 4},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertLogs("aivirteach.agent", level="WARNING"):
                    self.assertIsNone(normalize_usage(raw))


class UsageModelTests(unittest.TestCase):
    def test_usage_rejects_negative_and_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            usage(-1, 0, 0)
        with self.assertRaises(ValidationError):
            Usage(
                input_cache_hit_tokens=0,
                input_cache_miss_tokens=0,
                output_tokens=0,
                total_tokens=0,
            )

    def test_response_json_includes_usage_or_null(self) -> None:
        base = {
            "request_id": "a10beac8-d1db-4b1a-8df0-79aa8208e273",
            "status": "completed",
            "answer": "ok",
            "diagnosis": {},
            "course_alignment": {},
            "evidence": [],
            "suggested_actions": [],
            "limitations": [],
            "tool_trace": [],
        }
        without = DiagnoseResponse.model_validate(base).model_dump(mode="json")
        self.assertIn("usage", without)
        self.assertIsNone(without["usage"])

        with_usage = DiagnoseResponse.model_validate(
            {**base, "usage": DEEPSEEK_EXPECTED.model_dump()}
        ).model_dump(mode="json")
        self.assertEqual(
            with_usage["usage"],
            {
                "input_cache_hit_tokens": 640,
                "input_cache_miss_tokens": 360,
                "output_tokens": 200,
            },
        )


def _provider(handler: Any) -> tuple[OpenAICompatibleProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://provider.example/v1",
        api_key="secret",
        model="test-model",
        timeout_seconds=2,
        client=client,
    )
    return provider, client


class ProviderUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_parses_usage(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": DEEPSEEK_USAGE,
                },
            )

        provider, client = _provider(handler)
        try:
            turn = await provider.complete(
                messages=(ProviderMessage(role="user", content="hello"),), tools=()
            )
        finally:
            await client.aclose()
        self.assertEqual(turn.usage, DEEPSEEK_EXPECTED)

    async def test_complete_without_usage_still_succeeds(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": -3},
                },
            )

        provider, client = _provider(handler)
        try:
            with self.assertLogs("aivirteach.agent", level="WARNING"):
                turn = await provider.complete(
                    messages=(ProviderMessage(role="user", content="hello"),),
                    tools=(),
                )
        finally:
            await client.aclose()
        self.assertEqual(turn.text, "hi")
        self.assertIsNone(turn.usage)

    async def _stream(self, chunks: list[dict[str, Any]]) -> tuple[dict[str, Any], Any]:
        captured: dict[str, Any] = {}
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        body += "data: [DONE]\n\n"

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                content=body.encode(),
                headers={"Content-Type": "text/event-stream"},
            )

        provider, client = _provider(handler)
        try:
            events = [
                event
                async for event in provider.stream_complete(
                    messages=(ProviderMessage(role="user", content="hello"),),
                    tools=(),
                )
            ]
        finally:
            await client.aclose()
        done = next(event for event in events if isinstance(event, ProviderStreamDone))
        return captured, done

    async def test_stream_requests_usage_and_captures_final_usage_chunk(self) -> None:
        captured, done = await self._stream(
            [
                {
                    "choices": [{"delta": {"content": "Docker"}, "finish_reason": None}],
                    "usage": None,
                },
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": None,
                },
                {"choices": [], "usage": DEEPSEEK_USAGE},
            ]
        )
        self.assertEqual(captured["stream_options"], {"include_usage": True})
        self.assertEqual(done.finish_reason, "stop")
        self.assertEqual(done.usage, DEEPSEEK_EXPECTED)

    async def test_stream_without_usage_chunk_has_no_usage(self) -> None:
        _, done = await self._stream(
            [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        )
        self.assertIsNone(done.usage)

    async def test_non_stream_request_does_not_set_stream_options(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
            )

        provider, client = _provider(handler)
        try:
            with self.assertLogs("aivirteach.agent", level="WARNING"):
                await provider.complete(
                    messages=(ProviderMessage(role="user", content="hello"),),
                    tools=(),
                )
        finally:
            await client.aclose()
        self.assertNotIn("stream_options", captured)


def _tool_turn(call_id: str, turn_usage: Usage | None) -> ProviderTurn:
    return ProviderTurn(
        tool_calls=(
            ProviderToolCall(
                id=call_id,
                name="get_guest_service_status",
                arguments={"service": "docker.service"},
            ),
        ),
        usage=turn_usage,
    )


class StreamingFakeProvider(FakeProvider):
    """FakeProvider whose stream call reports a configurable usage."""

    def __init__(self, turns: list[ProviderTurn], stream_usage: Usage | None) -> None:
        super().__init__(turns)
        self._stream_usage = stream_usage

    async def stream_complete(self, *, messages: Any, tools: Any) -> Any:
        async for event in super().stream_complete(messages=messages, tools=tools):
            if isinstance(event, ProviderStreamDone):
                event = ProviderStreamDone(
                    finish_reason=event.finish_reason, usage=self._stream_usage
                )
            yield event


class ScriptedStreamProvider(FakeProvider):
    """FakeProvider whose stream replays fixed events; an exception is raised."""

    def __init__(self, turns: list[ProviderTurn], stream_events: list[Any]) -> None:
        super().__init__(turns)
        self._stream_events = stream_events

    async def stream_complete(self, *, messages: Any, tools: Any) -> Any:
        for event in self._stream_events:
            if isinstance(event, Exception):
                raise event
            yield event


class OrchestratorUsageTests(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, provider: FakeProvider) -> AgentOrchestrator:
        return AgentOrchestrator(
            settings=settings(), provider=provider, gateway=FakeGateway()
        )

    async def _diagnose(self, provider: FakeProvider) -> DiagnoseResponse:
        return await self._orchestrator(provider).diagnose(
            DiagnoseRequest.model_validate(request_payload())
        )

    async def test_usage_is_summed_across_tool_loop_calls(self) -> None:
        response = await self._diagnose(
            FakeProvider(
                [
                    _tool_turn("c1", usage(10, 20, 5)),
                    _tool_turn("c2", usage(30, 40, 6)),
                    ProviderTurn(text=FINAL_ASSESSMENT, usage=usage(1, 2, 3)),
                ]
            )
        )
        self.assertEqual(response.usage, usage(41, 62, 14))

    async def test_usage_is_none_when_no_call_reports_usage(self) -> None:
        response = await self._diagnose(
            FakeProvider([_tool_turn("c1", None), ProviderTurn(text=FINAL_ASSESSMENT)])
        )
        self.assertIsNone(response.usage)
        self.assertIsNone(response.model_dump(mode="json")["usage"])

    async def test_partial_usage_sums_reporting_calls_and_warns(self) -> None:
        provider = FakeProvider(
            [_tool_turn("c1", usage(10, 20, 5)), ProviderTurn(text=FINAL_ASSESSMENT)]
        )
        with self.assertLogs("aivirteach.agent", level="WARNING"):
            response = await self._diagnose(provider)
        self.assertEqual(response.usage, usage(10, 20, 5))

    async def test_usage_survives_turn_budget_finalization(self) -> None:
        orchestrator = AgentOrchestrator(
            settings=settings(max_reasoning_turns=1),
            provider=FakeProvider(
                [
                    _tool_turn("c1", usage(10, 20, 5)),
                    ProviderTurn(text=FINAL_ASSESSMENT, usage=usage(1, 1, 1)),
                ]
            ),
            gateway=FakeGateway(),
        )
        response = await orchestrator.diagnose(
            DiagnoseRequest.model_validate(request_payload())
        )
        self.assertEqual(response.usage, usage(11, 21, 6))

    async def test_stream_usage_is_added_to_diagnosis_usage(self) -> None:
        provider = StreamingFakeProvider(
            [_tool_turn("c1", usage(10, 20, 5)), ProviderTurn(text=FINAL_ASSESSMENT, usage=usage(1, 2, 3))],
            stream_usage=usage(100, 0, 7),
        )

        async def ignore(event: str, data: dict[str, Any]) -> None:
            return None

        response = await self._orchestrator(provider).diagnose_stream(
            DiagnoseRequest.model_validate(request_payload()), on_event=ignore
        )
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.usage, usage(111, 22, 15))

    async def test_stream_only_usage_when_diagnosis_calls_report_none(self) -> None:
        provider = StreamingFakeProvider(
            [ProviderTurn(text=FINAL_ASSESSMENT)], stream_usage=usage(0, 9, 4)
        )

        async def ignore(event: str, data: dict[str, Any]) -> None:
            return None

        with self.assertLogs("aivirteach.agent", level="WARNING"):
            response = await self._orchestrator(provider).diagnose_stream(
                DiagnoseRequest.model_validate(request_payload()), on_event=ignore
            )
        self.assertEqual(response.usage, usage(0, 9, 4))

    async def _stream_with(self, stream_events: list[Any]) -> DiagnoseResponse:
        provider = ScriptedStreamProvider(
            [_tool_turn("c1", usage(10, 20, 5)), ProviderTurn(text=FINAL_ASSESSMENT, usage=usage(1, 2, 3))],
            stream_events,
        )

        async def ignore(event: str, data: dict[str, Any]) -> None:
            return None

        return await self._orchestrator(provider).diagnose_stream(
            DiagnoseRequest.model_validate(request_payload()), on_event=ignore
        )

    async def test_incomplete_stream_fallback_still_counts_stream_usage(self) -> None:
        response = await self._stream_with(
            [
                ProviderTextDelta(text="部分回答"),
                ProviderStreamDone(finish_reason="length", usage=usage(100, 0, 7)),
            ]
        )
        self.assertEqual(response.status, "partial")
        self.assertIn("MODEL_STREAM_INCOMPLETE", response.limitations)
        self.assertEqual(response.usage, usage(111, 22, 15))

    async def test_failed_stream_fallback_keeps_diagnosis_usage_only(self) -> None:
        # The aborted stream never reported usage, so the total is incomplete
        # (warned) and only the diagnosis calls count.
        with self.assertLogs("aivirteach.agent", level="WARNING"):
            response = await self._stream_with(
                [ProviderTextDelta(text="部分回答"), ProviderError("stream broke")]
            )
        self.assertEqual(response.status, "partial")
        self.assertIn("MODEL_STREAM_ERROR", response.limitations)
        self.assertEqual(response.usage, usage(11, 22, 8))


class ApiUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnose_and_stream_result_carry_usage(self) -> None:
        from aivirteach_agent.app import create_app

        provider = StreamingFakeProvider(
            [ProviderTurn(text=FINAL_ASSESSMENT, usage=usage(10, 20, 5))],
            stream_usage=usage(1, 2, 3),
        )
        app = create_app(
            settings=settings(), provider=provider, gateway=FakeGateway()
        )
        headers = {"Authorization": "Bearer agent-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            plain = await client.post(
                "/v1/agent/diagnose", json=request_payload(), headers=headers
            )
            self.assertEqual(plain.status_code, 200)
            self.assertEqual(
                plain.json()["usage"],
                {
                    "input_cache_hit_tokens": 11,
                    "input_cache_miss_tokens": 22,
                    "output_tokens": 8,
                },
            )

            provider._turns.append(ProviderTurn(text=FINAL_ASSESSMENT, usage=usage(10, 20, 5)))
            streamed = await client.post(
                "/v1/agent/diagnose/stream", json=request_payload(), headers=headers
            )
        self.assertEqual(streamed.status_code, 200)
        result = next(
            json.loads(line[5:])
            for block in streamed.text.split("\n\n")
            if "event: result" in block
            for line in block.splitlines()
            if line.startswith("data:")
        )
        self.assertEqual(
            result["response"]["usage"],
            {
                "input_cache_hit_tokens": 11,
                "input_cache_miss_tokens": 22,
                "output_tokens": 8,
            },
        )


if __name__ == "__main__":
    unittest.main()
