import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from aivirteach_agent.app import build_provider, create_app
from aivirteach_agent.config import Settings
from aivirteach_agent.models import DiagnoseRequest, ToolName
from aivirteach_agent.orchestrator import AgentOrchestrator, _parse_assessment
from aivirteach_agent.prompts import SYSTEM_PROMPT
from aivirteach_agent.providers import (
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderMessage,
    ProviderStreamDone,
    ProviderTextDelta,
    ProviderTool,
    ProviderToolCall,
    ProviderTurn,
)
from aivirteach_agent.security import sanitize_value
from aivirteach_agent.tools import ToolPolicyError, validate_tool_call


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "agent_token": "agent-token",
        "gateway_url": "http://gateway",
        "diagnostic_token": "diagnostic-token",
        "model_provider": "fake",
        "model_base_url": "",
        "model_api_key": "",
        "model_name": "",
        "total_timeout_seconds": 5,
        "model_timeout_seconds": 2,
        "tool_timeout_seconds": 2,
        "max_reasoning_turns": 3,
        "max_tool_calls": 6,
        "max_tool_output_chars": 32_768,
        "max_concurrent_requests": 2,
    }
    values.update(overrides)
    return Settings(**values)


def request_payload() -> dict[str, Any]:
    return {
        "request_id": "a10beac8-d1db-4b1a-8df0-79aa8208e273",
        "lab_id": "lab-001",
        "question": "Docker 服务为什么没有运行？",
        "response_language": "zh-CN",
        "course": {
            "course_id": "docker-course",
            "version": 1,
            "title": "Docker basics",
        },
        "current_step": {
            "module_id": "runtime",
            "lesson_id": "install-docker",
            "sequence": 3,
            "title": "Install Docker",
            "expected_result": "docker.service is active",
        },
        "diagnostic_scope": {
            "workspace_root": "/home/learner/course",
            "allowed_tools": ["get_guest_service_status"],
            "allowed_relative_paths": [],
            "allowed_services": ["docker.service"],
            "allowed_containers": [],
            "allowed_ports": [],
            "allowed_external_hosts": [],
            "allowed_runtimes": [],
        },
    }


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ToolName, dict[str, Any]]] = []

    async def query(
        self, *, lab_id: str, tool: ToolName, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((lab_id, tool, parameters))
        return {
            "summary": "docker.service is inactive",
            "data": {"ActiveState": "inactive", "token": "secret-value"},
        }

    async def aclose(self) -> None:
        return None


class ProgressSnapshotPromptTests(unittest.TestCase):
    def test_agent_uses_server_snapshot_instead_of_rechecking_completion(self) -> None:
        self.assertIn("learner_state.vm_progress", SYSTEM_PROMPT)
        self.assertIn("instead of calling tools merely to re-check course completion", SYSTEM_PROMPT)


class AgentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cors_environment = patch.dict(
            os.environ,
            {"AIVIRTEACH_AGENT_CORS_ORIGINS": "http://127.0.0.1:8780"},
        )
        self.cors_environment.start()
        self.addCleanup(self.cors_environment.stop)
        self.gateway = FakeGateway()
        self.provider = FakeProvider()
        self.app = create_app(
            settings=settings(), provider=self.provider, gateway=self.gateway
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_health_and_readiness(self) -> None:
        health = await self.client.get("/health")
        ready = await self.client.get("/ready")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["provider"], "fake")

    async def test_diagnose_requires_agent_token(self) -> None:
        response = await self.client.post("/v1/agent/diagnose", json=request_payload())
        self.assertEqual(response.status_code, 401)

        stream_response = await self.client.post(
            "/v1/agent/diagnose/stream",
            json=request_payload(),
        )
        self.assertEqual(stream_response.status_code, 401)

    async def test_swagger_origin_is_allowed_by_cors(self) -> None:
        response = await self.client.options(
            "/v1/agent/diagnose",
            headers={
                "Origin": "http://127.0.0.1:8780",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://127.0.0.1:8780",
        )

    async def test_agent_openapi_uses_standard_bearer_scheme(self) -> None:
        schema = self.app.openapi()
        operation = schema["paths"]["/v1/agent/diagnose"]["post"]
        self.assertEqual(operation["security"], [{"AgentBearer": []}])
        self.assertIn("AgentBearer", schema["components"]["securitySchemes"])
        parameter_names = {
            parameter.get("name", "").lower()
            for parameter in operation.get("parameters", [])
        }
        self.assertNotIn("authorization", parameter_names)
        stream_operation = schema["paths"]["/v1/agent/diagnose/stream"]["post"]
        self.assertEqual(stream_operation["security"], [{"AgentBearer": []}])
        self.assertIn(
            "text/event-stream",
            stream_operation["responses"]["200"]["content"],
        )

    async def test_fake_provider_smoke_response(self) -> None:
        response = await self.client.post(
            "/v1/agent/diagnose",
            headers={"Authorization": "Bearer agent-token"},
            json=request_payload(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertIn("FAKE_MODEL_PROVIDER", response.json()["limitations"])

    async def test_sse_stream_emits_progress_and_validated_result(self) -> None:
        response = await self.client.post(
            "/v1/agent/diagnose/stream",
            headers={"Authorization": "Bearer agent-token"},
            json=request_payload(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        events = _parse_sse(response.text)
        event_names = [event for event, _ in events]
        self.assertEqual(
            event_names[:8],
            [
                "accepted",
                "started",
                "context_ready",
                "reasoning_started",
                "reasoning_finished",
                "assessment_validating",
                "assessment_validated",
                "answer_started",
            ],
        )
        self.assertEqual(event_names[-3:], ["answer_finished", "result", "done"])
        self.assertGreater(event_names.count("assistant_delta"), 1)
        sequences = [payload["sequence"] for _, payload in events]
        self.assertEqual(sequences, list(range(1, len(events) + 1)))
        self.assertTrue(
            all(
                payload["schema_version"] == 1
                and payload["request_id"] == request_payload()["request_id"]
                for _, payload in events
            )
        )
        deltas = [
            payload["delta"]
            for event, payload in events
            if event == "assistant_delta"
        ]
        self.assertTrue(all(isinstance(delta, str) and delta for delta in deltas))
        self.assertNotIn('{"answer"', "".join(deltas))
        result = next(payload for event, payload in events if event == "result")
        self.assertEqual(result["response"]["status"], "completed")
        self.assertIn("FAKE_MODEL_PROVIDER", result["response"]["limitations"])
        self.assertEqual("".join(deltas), result["response"]["answer"])
        done = next(payload for event, payload in events if event == "done")
        self.assertEqual(done["status"], result["response"]["status"])


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_tool_call_collects_real_evidence(self) -> None:
        final = {
            "diagnosis": {
                "summary": "docker.service is inactive",
                "probable_causes": ["Docker 未启动"],
                "confidence": "high",
            },
            "course_alignment": {
                "expected": ["docker.service active"],
                "observed": ["docker.service inactive"],
            },
            "evidence_ids": ["obs-001"],
            "suggested_actions": [
                {"title": "检查安装步骤", "detail": "请重新核对课程中的安装命令。"}
            ],
            "limitations": [],
        }
        provider = FakeProvider(
            [
                ProviderTurn(
                    tool_calls=(
                        ProviderToolCall(
                            id="call-1",
                            name="get_guest_service_status",
                            arguments={"service": "docker.service"},
                        ),
                    )
                ),
                ProviderTurn(text=json.dumps(final, ensure_ascii=False)),
            ]
        )
        gateway = FakeGateway()
        orchestrator = AgentOrchestrator(
            settings=settings(), provider=provider, gateway=gateway
        )
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(event: str, data: dict[str, Any]) -> None:
            events.append((event, data))

        response = await orchestrator.diagnose(
            DiagnoseRequest.model_validate(request_payload()),
            on_event=capture,
        )

        self.assertEqual(response.status, "completed")
        self.assertEqual(response.evidence[0].id, "obs-001")
        self.assertEqual(gateway.calls[0][1], ToolName.GET_GUEST_SERVICE_STATUS)
        self.assertEqual(response.tool_trace[0].status, "ok")
        self.assertIn(
            ("tool_started", {"tool": "get_guest_service_status"}),
            events,
        )
        finished = next(data for event, data in events if event == "tool_finished")
        self.assertEqual(finished["status"], "ok")
        self.assertEqual(finished["observation_id"], "obs-001")

    async def test_course_scope_denies_resource_before_gateway(self) -> None:
        payload = request_payload()
        payload["diagnostic_scope"]["allowed_services"] = []
        provider = FakeProvider(
            [
                ProviderTurn(
                    tool_calls=(
                        ProviderToolCall(
                            id="call-1",
                            name="get_guest_service_status",
                            arguments={"service": "docker.service"},
                        ),
                    )
                ),
                ProviderTurn(
                    text=json.dumps(
                        {
                            "diagnosis": {"summary": "未知", "confidence": "low"},
                            "course_alignment": {"expected": [], "observed": []},
                            "evidence_ids": [],
                            "suggested_actions": [],
                            "limitations": [],
                        }
                    )
                ),
            ]
        )
        gateway = FakeGateway()
        response = await AgentOrchestrator(
            settings=settings(), provider=provider, gateway=gateway
        ).diagnose(DiagnoseRequest.model_validate(payload))

        self.assertEqual(gateway.calls, [])
        self.assertEqual(response.status, "partial")
        self.assertEqual(response.tool_trace[0].error_code, "SERVICE_NOT_ALLOWED")

    async def test_duplicate_tool_call_uses_request_cache(self) -> None:
        final = {
            "diagnosis": {"summary": "状态已知", "confidence": "medium"},
            "course_alignment": {"expected": [], "observed": []},
            "evidence_ids": ["obs-001"],
            "suggested_actions": [],
            "limitations": [],
        }
        call = ProviderToolCall(
            id="call-1",
            name="get_guest_service_status",
            arguments={"service": "docker.service"},
        )
        provider = FakeProvider(
            [
                ProviderTurn(tool_calls=(call,)),
                ProviderTurn(
                    tool_calls=(
                        ProviderToolCall(
                            id="call-2", name=call.name, arguments=call.arguments
                        ),
                    )
                ),
                ProviderTurn(text=json.dumps(final)),
            ]
        )
        gateway = FakeGateway()
        response = await AgentOrchestrator(
            settings=settings(), provider=provider, gateway=gateway
        ).diagnose(DiagnoseRequest.model_validate(request_payload()))

        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual([item.status for item in response.tool_trace], ["ok", "cached"])


class PolicyTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self) -> None:
        scope = DiagnoseRequest.model_validate(request_payload()).diagnostic_scope
        scope.allowed_tools.add(ToolName.READ_COURSE_FILE)
        scope.allowed_relative_paths = ["."]
        with self.assertRaises(ToolPolicyError) as raised:
            validate_tool_call(
                "read_course_file", {"path": "../../etc/passwd"}, scope
            )
        self.assertEqual(raised.exception.code, "INVALID_TOOL_ARGUMENTS")

    def test_secret_redaction_and_truncation(self) -> None:
        cleaned, truncated, count = sanitize_value(
            {"log": "password=hunter2\n" + ("x" * 100)}, max_chars=40
        )
        self.assertTrue(truncated)
        self.assertGreaterEqual(count, 1)
        self.assertNotIn("hunter2", str(cleaned))


class OpenAICompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_complete_yields_only_visible_text_deltas(self) -> None:
        captured: dict[str, Any] = {}
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "Docker ",
                            "reasoning_content": "private reasoning",
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "服务未运行。"},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [], "usage": {"total_tokens": 12}},
            {
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            },
        ]
        body = "".join(
            f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            for chunk in chunks
        ) + "data: [DONE]\n\n"

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                content=body.encode(),
                headers={"Content-Type": "text/event-stream"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            base_url="https://provider.example/v1",
            api_key="secret",
            model="test-model",
            timeout_seconds=2,
            thinking="disabled",
            client=client,
        )
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

        deltas = [
            event.text for event in events if isinstance(event, ProviderTextDelta)
        ]
        done = next(event for event in events if isinstance(event, ProviderStreamDone))
        self.assertTrue(captured["stream"])
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", captured)
        self.assertNotIn("tool_choice", captured)
        self.assertEqual(deltas, ["Docker ", "服务未运行。"])
        self.assertNotIn("private reasoning", "".join(deltas))
        self.assertEqual(done.finish_reason, "stop")

    async def test_configured_thinking_mode_is_sent_to_provider(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            base_url="https://provider.example/v1",
            api_key="secret",
            model="test-model",
            timeout_seconds=2,
            thinking="disabled",
            client=client,
        )
        try:
            response = await provider.complete(
                messages=(ProviderMessage(role="user", content="hello"),),
                tools=(
                    ProviderTool(
                        name="inspect",
                        description="Inspect state",
                        input_schema={"type": "object", "properties": {}},
                    ),
                ),
            )
        finally:
            await client.aclose()

        self.assertEqual(response.text, "ok")
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(captured["tool_choice"], "auto")

    def test_build_provider_forwards_thinking_setting(self) -> None:
        provider = build_provider(
            settings(
                model_provider="openai_compatible",
                model_base_url="https://provider.example/v1",
                model_api_key="secret",
                model_name="test-model",
                model_thinking="disabled",
            )
        )
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertEqual(provider._thinking, "disabled")

    def test_invalid_thinking_setting_fails_readiness(self) -> None:
        errors = settings(
            model_provider="openai_compatible",
            model_base_url="https://provider.example/v1",
            model_api_key="secret",
            model_name="test-model",
            model_thinking="sometimes",
        ).readiness_errors()
        self.assertIn(
            "AIVIRTEACH_MODEL_THINKING must be enabled or disabled", errors
        )


class ModelOutputParsingTests(unittest.TestCase):
    def test_extracts_valid_assessment_after_provider_preamble(self) -> None:
        draft = _parse_assessment(
            "I have enough evidence.\n\n"
            + json.dumps(
                {
                    "diagnosis": {
                        "summary": "状态正常",
                        "probable_causes": [],
                        "confidence": "high",
                    },
                    "course_alignment": {"expected": [], "observed": []},
                    "evidence_ids": ["obs-001"],
                    "suggested_actions": [],
                    "limitations": "未检查应用端口。",
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(draft.diagnosis.summary, "状态正常")
        self.assertEqual(draft.limitations, ["未检查应用端口。"])


def _parse_sse(value: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in value.strip().split("\n\n"):
        if not block or block.startswith(":"):
            continue
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line
        )
        events.append((fields["event"], json.loads(fields["data"])))
    return events


if __name__ == "__main__":
    unittest.main()
