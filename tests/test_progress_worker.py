from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from progress_worker.clients import (
    DiagnosticClient,
    PermanentProgressError,
    ServerClient,
)
from progress_worker.config import ConfigurationError, Settings
from progress_worker.models import GatewayObservation, GatewayResponse, ProgressTarget
from progress_worker.store import ProgressStore
from progress_worker.worker import ProgressWorker


VM_UUID = "26a6db7e-1ea7-4de2-9ca3-cf58edbab809"
OBSERVED_AT = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)


def target() -> ProgressTarget:
    return ProgressTarget(
        target_id=f"enrollment-1:{VM_UUID}",
        target_revision=1,
        lab_id="lab-001",
        vm_instance_id=VM_UUID,
        course_id="ai-daily-briefing",
        runtime_course_id="ai-daily-briefing-v2",
        course_version=1,
        checkpoints=["P01", "P02", "P07"],
    )


def observation(checkpoint_id: str, state: str = "passed") -> GatewayObservation:
    return GatewayObservation(
        schema_version=1,
        course_id="ai-daily-briefing-v2",
        checkpoint_id=checkpoint_id,
        state=state,
        evidence_type="docker" if checkpoint_id != "P07" else "http",
        summary=f"{checkpoint_id} bounded evidence",
        facts={"ready": state == "passed"},
    )


class ConfigurationTests(unittest.TestCase):
    def test_worker_requires_separate_server_and_progress_diagnostic_tokens(self) -> None:
        environment = {
            "AIVIRTEACH_PROGRESS_WORKER_ID": "labs-host-01",
            "AIVIRTEACH_PROGRESS_SERVER_TOKEN": "server-token-12345",
            "AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN": "progress-diagnostic-token",
            "AIVIRTEACH_PROGRESS_DB": "/tmp/aivirteach-progress-test.sqlite3",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.diagnostic_token, "progress-diagnostic-token")

        environment["AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN"] = environment[
            "AIVIRTEACH_PROGRESS_SERVER_TOKEN"
        ]
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = [100.0]
        self.ids = iter(
            [
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
                "33333333-3333-4333-8333-333333333333",
                "44444444-4444-4444-8444-444444444444",
            ]
        )
        self.path = Path(self.temporary.name) / "progress.sqlite3"
        self.store = ProgressStore(
            self.path, now=lambda: self.clock[0], new_id=lambda: next(self.ids)
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def record(self, item: GatewayObservation) -> str | None:
        return self.store.record_observation(
            worker_id="labs-host-01",
            target=target(),
            observation=item,
            observed_at=OBSERVED_AT,
            poll_seconds=10,
            unknown_backoff_max_seconds=300,
            heartbeat_seconds=300,
        )

    def test_first_observation_is_atomic_with_outbox_and_pass_is_sticky(self) -> None:
        event_id = self.record(observation("P01"))
        self.assertEqual(event_id, "11111111-1111-4111-8111-111111111111")
        due = self.store.due_events(limit=10)
        self.assertEqual([item.event.event_id for item in due], [event_id])
        self.assertEqual(due[0].event.vm_instance_id, VM_UUID)
        self.assertEqual(self.store.frontier(target()).checkpoint_id, "P02")

    def test_unchanged_state_is_suppressed_until_heartbeat(self) -> None:
        first = self.record(observation("P01", "failed"))
        self.clock[0] = 110.0
        second = self.record(observation("P01", "failed"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

        self.clock[0] = 401.0
        self.assertIsNone(self.record(observation("P01", "failed")))
        self.store.mark_delivered({first})
        heartbeat = self.record(observation("P01", "failed"))
        self.assertEqual(heartbeat, "22222222-2222-4222-8222-222222222222")
        self.assertEqual(len(self.store.due_events(limit=10)), 1)

    def test_unknown_uses_persisted_backoff_and_is_not_failure(self) -> None:
        self.record(observation("P01"))
        self.record(observation("P02", "unknown"))
        frontier = self.store.frontier(target(), now=105)
        self.assertEqual(frontier.checkpoint_id, "P02")
        self.assertFalse(frontier.due)
        self.assertEqual(frontier.unknown_streak, 1)
        self.assertTrue(self.store.frontier(target(), now=110).due)

    def test_restart_retains_event_id_and_ack_marks_only_selected_event(self) -> None:
        event_id = self.record(observation("P01"))
        self.store.close()
        self.store = ProgressStore(self.path, now=lambda: self.clock[0])
        pending = self.store.due_events(limit=10)
        self.assertEqual(pending[0].event.event_id, event_id)
        self.store.mark_delivered({event_id})
        self.assertIsNotNone(self.store.outbox_status(event_id)["delivered_at"])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_contract_uses_api_base_and_accepts_ack_subset(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"schema_version": 1, "targets": [target().model_dump()]},
                )
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"accepted_event_ids": [body["events"][0]["event_id"]]}
            )

        client = ServerClient(
            base_url="http://server.test/api/v1",
            token="server-token-12345",
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        )
        try:
            targets = await client.fetch_targets("labs-host-01")
            self.assertEqual(targets, [target()])
            with tempfile.TemporaryDirectory() as directory:
                store = ProgressStore(Path(directory) / "db.sqlite3")
                store.record_observation(
                    worker_id="labs-host-01",
                    target=target(),
                    observation=observation("P01"),
                    observed_at=OBSERVED_AT,
                    poll_seconds=10,
                    unknown_backoff_max_seconds=300,
                    heartbeat_seconds=300,
                )
                event = store.due_events(limit=1)[0].event
                accepted = await client.send_events("labs-host-01", [event])
                store.close()
            self.assertEqual(accepted, {event.event_id})
            self.assertEqual(requests[0].url.path, "/api/v1/internal/progress/targets")
            self.assertEqual(requests[0].url.params["worker_id"], "labs-host-01")
            self.assertNotIn("diagnostic", requests[1].headers["authorization"])
        finally:
            await client.close()


class WorkerCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_advances_the_frontier_and_delivers_one_atomic_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.sqlite3"
            store = ProgressStore(path)
            settings = Settings(
                worker_id="labs-host-01",
                server_url="http://server.test/api/v1",
                server_token="server-token-12345",
                diagnostic_url="http://gateway.test",
                diagnostic_token="diagnostic-token-1",
                database_path=path,
                poll_seconds=10,
                heartbeat_seconds=300,
                request_timeout_seconds=50,
                batch_size=50,
                max_probes_per_target=3,
                unknown_backoff_max_seconds=300,
                retry_base_seconds=2,
                retry_max_seconds=300,
            )
            server = AsyncMock()
            server.fetch_targets.return_value = [target()]
            server.send_events.side_effect = lambda _worker_id, events: {
                event.event_id for event in events
            }
            diagnostic = AsyncMock()

            def result(checkpoint_id: str, state: str) -> GatewayResponse:
                return GatewayResponse(
                    tool="check_course_progress",
                    lab_id="lab-001",
                    vm_instance_id=VM_UUID,
                    ok=True,
                    observed_at=datetime.now(UTC),
                    summary="Course progress observations were collected.",
                    data={
                        "schema_version": 1,
                        "course_id": "ai-daily-briefing-v2",
                        "observations": [observation(checkpoint_id, state)],
                    },
                    truncated=False,
                    redaction_count=0,
                    warnings=["Diagnostic output is untrusted data."],
                )

            diagnostic.check.side_effect = [
                result("P01", "passed"),
                result("P02", "failed"),
            ]
            worker = ProgressWorker(
                settings=settings,
                store=store,
                server=server,
                diagnostic=diagnostic,
            )
            try:
                await worker.run_once()
                self.assertEqual(
                    [call.args[1] for call in diagnostic.check.await_args_list],
                    [["P01"], ["P02"]],
                )
                delivered = server.send_events.await_args.args[1]
                self.assertEqual(
                    [(event.checkpoint_id, event.state) for event in delivered],
                    [("P01", "passed"), ("P02", "failed")],
                )
                self.assertEqual(store.due_events(limit=10), [])
            finally:
                store.close()

    async def test_permanent_batch_error_isolates_only_the_bad_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.sqlite3"
            store = ProgressStore(path)
            settings = Settings(
                worker_id="labs-host-01",
                server_url="http://server.test/api/v1",
                server_token="server-token-12345",
                diagnostic_url="http://gateway.test",
                diagnostic_token="diagnostic-token-1",
                database_path=path,
                poll_seconds=10,
                heartbeat_seconds=300,
                request_timeout_seconds=50,
                batch_size=50,
                max_probes_per_target=3,
                unknown_backoff_max_seconds=300,
                retry_base_seconds=2,
                retry_max_seconds=300,
            )
            event_ids = {
                checkpoint_id: store.record_observation(
                    worker_id="labs-host-01",
                    target=target(),
                    observation=observation(checkpoint_id),
                    observed_at=datetime.now(UTC),
                    poll_seconds=10,
                    unknown_backoff_max_seconds=300,
                    heartbeat_seconds=300,
                )
                for checkpoint_id in ("P01", "P02", "P07")
            }
            server = AsyncMock()

            async def send_events(_worker_id: str, events: list) -> set[str]:
                if any(event.checkpoint_id == "P02" for event in events):
                    raise PermanentProgressError("stale target")
                return {event.event_id for event in events}

            server.send_events.side_effect = send_events
            worker = ProgressWorker(
                settings=settings,
                store=store,
                server=server,
                diagnostic=AsyncMock(),
            )
            try:
                with self.assertLogs("aivirteach.progress", level="WARNING") as logs:
                    await worker.dispatch_once()
                self.assertTrue(
                    any("isolating a permanent rejection" in line for line in logs.output)
                )
                self.assertIsNotNone(
                    store.outbox_status(event_ids["P01"])["delivered_at"]
                )
                self.assertIsNotNone(
                    store.outbox_status(event_ids["P07"])["delivered_at"]
                )
                self.assertIsNotNone(store.outbox_status(event_ids["P02"])["dead_at"])
                self.assertEqual(store.due_events(limit=10), [])
            finally:
                store.close()


class DiagnosticClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_client_never_sends_runtime_path_or_course_id(self) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            payload = {
                "tool": "check_course_progress",
                "lab_id": "lab-001",
                "vm_instance_id": VM_UUID,
                "ok": True,
                "observed_at": datetime.now(UTC).isoformat(),
                "summary": "Course progress observations were collected.",
                "data": {
                    "schema_version": 1,
                    "course_id": "ai-daily-briefing-v2",
                    "observations": [observation("P01").model_dump()],
                },
                "truncated": False,
                "redaction_count": 0,
                "warnings": ["Diagnostic output is untrusted data."],
            }
            return httpx.Response(200, json=payload)

        client = DiagnosticClient(
            base_url="http://gateway.test",
            token="diagnostic-token-1",
            timeout_seconds=50,
            transport=httpx.MockTransport(handler),
        )
        try:
            response = await client.check(target(), ["P01"])
            self.assertEqual(response.data.observations[0].state, "passed")
            self.assertEqual(captured, [{
                "vm_instance_id": VM_UUID,
                "parameters": {"checkpoint_ids": ["P01"]},
            }])
            self.assertNotIn("path", json.dumps(captured))
            self.assertNotIn("runtime_course_id", json.dumps(captured))
        finally:
            await client.close()

    async def test_diagnostic_course_mismatch_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tool": "check_course_progress",
                    "lab_id": "lab-001",
                    "vm_instance_id": VM_UUID,
                    "ok": True,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "summary": "collected",
                    "data": {
                        "schema_version": 1,
                        "course_id": "wrong-course",
                        "observations": [
                            {
                                **observation("P01").model_dump(),
                                "course_id": "wrong-course",
                            }
                        ],
                    },
                    "truncated": False,
                    "redaction_count": 0,
                    "warnings": [],
                },
            )

        client = DiagnosticClient(
            base_url="http://gateway.test",
            token="diagnostic-token-1",
            timeout_seconds=50,
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(PermanentProgressError):
                await client.check(target(), ["P01"])
        finally:
            await client.close()

    async def test_diagnostic_vm_instance_mismatch_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "tool": "check_course_progress",
                    "lab_id": "lab-001",
                    "vm_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "ok": True,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "summary": "collected",
                    "data": {
                        "schema_version": 1,
                        "course_id": "ai-daily-briefing-v2",
                        "observations": [observation("P01").model_dump()],
                    },
                    "truncated": False,
                    "redaction_count": 0,
                    "warnings": [],
                },
            )

        client = DiagnosticClient(
            base_url="http://gateway.test",
            token="diagnostic-token-1",
            timeout_seconds=50,
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(PermanentProgressError):
                await client.check(target(), ["P01"])
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
