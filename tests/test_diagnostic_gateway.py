import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import diagnostic_gateway as diagnostics


VM_UUID = "26a6db7e-1ea7-4de2-9ca3-cf58edbab809"


def progress_payload(
    checkpoint_ids: tuple[str, ...] = ("P01", "P07"),
    *,
    course_id: str = "ai-daily-briefing-v2",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "course_id": course_id,
        "observations": [
            {
                "schema_version": 1,
                "course_id": course_id,
                "checkpoint_id": checkpoint_id,
                "state": "passed" if checkpoint_id == "P01" else "unknown",
                "evidence_type": "docker" if checkpoint_id == "P01" else "http",
                "summary": "Bounded progress evidence.",
                "facts": {"observed": True},
            }
            for checkpoint_id in checkpoint_ids
        ],
    }


class DiagnosticGatewayApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["AIVIRTEACH_DIAGNOSTIC_TOKEN"] = "diagnostic-token"
        os.environ.pop("AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN", None)
        app = FastAPI()
        app.include_router(diagnostics.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        os.environ.pop("AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN", None)

    async def test_endpoint_has_separate_bearer_auth(self) -> None:
        response = await self.client.post(
            "/v1/diagnostics/lab-001/tools/get_vm_status",
            json={"parameters": {}},
        )
        self.assertEqual(response.status_code, 401)

    async def test_progress_token_can_only_call_the_fixed_progress_tool(self) -> None:
        os.environ["AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN"] = "progress-only-token"
        denied = await self.client.post(
            "/v1/diagnostics/lab-001/tools/get_guest_journal",
            headers={"Authorization": "Bearer progress-only-token"},
            json={"parameters": {"service": "docker.service"}},
        )
        self.assertEqual(denied.status_code, 403)

        payload = progress_payload(("P01",))
        with (
            patch.object(
                diagnostics, "_ensure_domain", AsyncMock(return_value=VM_UUID)
            ),
            patch.object(
                diagnostics,
                "_guest_exec",
                AsyncMock(
                    return_value=diagnostics.GuestResult(0, json.dumps(payload), "")
                ),
            ),
        ):
            allowed = await self.client.post(
                "/v1/diagnostics/lab-001/tools/check_course_progress",
                headers={"Authorization": "Bearer progress-only-token"},
                json={
                    "vm_instance_id": VM_UUID,
                    "parameters": {"checkpoint_ids": ["P01"]},
                },
            )
        self.assertEqual(allowed.status_code, 200)

    async def test_endpoint_returns_bounded_contract(self) -> None:
        with (
            patch.object(
                diagnostics, "_ensure_domain", AsyncMock(return_value=VM_UUID)
            ),
            patch.object(
                diagnostics,
                "_collect",
                AsyncMock(return_value=("collected", {"state": "running"}, False, 0)),
            ),
        ):
            response = await self.client.post(
                "/v1/diagnostics/lab-001/tools/get_vm_status",
                headers={"Authorization": "Bearer diagnostic-token"},
                json={"parameters": {}},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tool"], "get_vm_status")
        self.assertNotIn("vm_instance_id", body)
        self.assertEqual(body["data"]["state"], "running")
        self.assertIn("untrusted", body["warnings"][0].lower())

    async def test_invalid_lab_id_is_rejected_before_libvirt(self) -> None:
        with patch.object(diagnostics, "_ensure_domain", AsyncMock()) as ensure:
            response = await self.client.post(
                "/v1/diagnostics/not valid/tools/get_vm_status",
                headers={"Authorization": "Bearer diagnostic-token"},
                json={"parameters": {}},
            )
        self.assertEqual(response.status_code, 422)
        ensure.assert_not_awaited()

    def test_virsh_uses_the_libvirt_socket_without_sudo(self) -> None:
        self.assertEqual(
            diagnostics._virsh_argv("dominfo", "lab-001"),
            ["virsh", "--connect", "qemu:///system", "dominfo", "lab-001"],
        )

    async def test_domain_identity_is_resolved_as_a_normalized_libvirt_uuid(self) -> None:
        with patch.object(
            diagnostics, "_virsh", AsyncMock(return_value=VM_UUID.upper())
        ) as virsh:
            result = await diagnostics._ensure_domain("lab-001")

        self.assertEqual(result, VM_UUID)
        virsh.assert_awaited_once_with("domuuid", "lab-001")

    async def test_course_progress_endpoint_returns_only_normalized_data(self) -> None:
        payload = progress_payload()
        runner = AsyncMock(
            return_value=diagnostics.GuestResult(0, json.dumps(payload), "")
        )
        with (
            patch.object(
                diagnostics, "_ensure_domain", AsyncMock(return_value=VM_UUID)
            ),
            patch.object(diagnostics, "_guest_exec", runner),
        ):
            response = await self.client.post(
                "/v1/diagnostics/lab-001/tools/check_course_progress",
                headers={"Authorization": "Bearer diagnostic-token"},
                json={
                    "vm_instance_id": VM_UUID,
                    "parameters": {"checkpoint_ids": ["P01", "P07"]},
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"], payload)
        self.assertEqual(body["vm_instance_id"], VM_UUID)
        self.assertNotIn("stdout", json.dumps(body["data"]))
        self.assertNotIn("stderr", json.dumps(body["data"]))
        runner.assert_awaited_once_with(
            VM_UUID,
            "/usr/local/bin/aivirteach-check-progress",
            ["P01", "P07"],
            timeout_seconds=45,
        )

    async def test_course_progress_rejects_a_replaced_vm_before_guest_exec(self) -> None:
        runner = AsyncMock()
        with (
            patch.object(
                diagnostics, "_ensure_domain", AsyncMock(return_value=VM_UUID)
            ),
            patch.object(diagnostics, "_guest_exec", runner),
        ):
            response = await self.client.post(
                "/v1/diagnostics/lab-001/tools/check_course_progress",
                headers={"Authorization": "Bearer diagnostic-token"},
                json={
                    "vm_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "parameters": {"checkpoint_ids": ["P01"]},
                },
            )

        self.assertEqual(response.status_code, 409)
        runner.assert_not_awaited()

    async def test_course_progress_recursively_redacts_structured_facts(self) -> None:
        payload = progress_payload(("P01",))
        observation = payload["observations"][0]
        observation["summary"] = "password=summary-secret"
        observation["facts"] = {
            "password": "fact-secret",
            "nested": {
                "message": "token=nested-secret",
                "safe": ["visible", {"api_key": "key-secret"}],
            },
        }
        with (
            patch.object(
                diagnostics, "_ensure_domain", AsyncMock(return_value=VM_UUID)
            ),
            patch.object(
                diagnostics,
                "_guest_exec",
                AsyncMock(
                    return_value=diagnostics.GuestResult(
                        0, json.dumps(payload), ""
                    )
                ),
            ),
        ):
            response = await self.client.post(
                "/v1/diagnostics/lab-001/tools/check_course_progress",
                headers={"Authorization": "Bearer diagnostic-token"},
                json={
                    "vm_instance_id": VM_UUID,
                    "parameters": {"checkpoint_ids": ["P01"]},
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        encoded = json.dumps(body["data"])
        self.assertNotIn("summary-secret", encoded)
        self.assertNotIn("fact-secret", encoded)
        self.assertNotIn("nested-secret", encoded)
        self.assertNotIn("key-secret", encoded)
        self.assertIn("visible", encoded)
        self.assertEqual(body["redaction_count"], 4)


class GuestExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_exec_uses_fixed_qga_payload_and_decodes_output(self) -> None:
        encoded = base64.b64encode(b"ActiveState=active\n").decode()
        qga = AsyncMock(
            side_effect=[
                {"pid": 42},
                {"exited": True, "exitcode": 0, "out-data": encoded},
            ]
        )
        with patch.object(diagnostics, "_qga", qga):
            result = await diagnostics._guest_exec(
                "lab-001", "/usr/bin/systemctl", ["show", "docker.service"]
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "ActiveState=active")
        first_payload = qga.await_args_list[0].args[1]
        self.assertEqual(first_payload["arguments"]["path"], "/usr/bin/systemctl")
        self.assertNotIn("shell", first_payload["arguments"])

    async def test_guest_exec_rejects_missing_exit_code(self) -> None:
        qga = AsyncMock(
            side_effect=[
                {"pid": 42},
                {"exited": True, "out-data": base64.b64encode(b"{}").decode()},
            ]
        )
        with (
            patch.object(diagnostics, "_qga", qga),
            self.assertRaises(Exception) as raised,
        ):
            await diagnostics._guest_exec("lab-001", "/usr/bin/true")

        self.assertEqual(getattr(raised.exception, "status_code", None), 502)

    async def test_journal_parameters_map_to_fixed_argv(self) -> None:
        runner = AsyncMock(
            return_value=diagnostics.GuestResult(0, "journal output", "")
        )
        with patch.object(diagnostics, "_guest_exec", runner):
            await diagnostics._collect(
                diagnostics.DiagnosticTool.GET_GUEST_JOURNAL,
                "lab-001",
                {"service": "docker.service", "lines": 20, "since_minutes": 15},
            )

        path = runner.await_args.args[1]
        argv = runner.await_args.args[2]
        self.assertEqual(path, "/usr/bin/journalctl")
        self.assertIn("--lines=20", argv)
        self.assertIn("--since=-15min", argv)
        self.assertEqual(argv[-1], "docker.service")

    async def test_workspace_traversal_is_rejected(self) -> None:
        with self.assertRaises(Exception) as raised:
            diagnostics._workspace_parameters(
                {
                    "workspace_root": "/home/learner/course",
                    "path": "../../etc/shadow",
                }
            )
        self.assertEqual(getattr(raised.exception, "status_code", None), 422)

    async def test_course_progress_rejects_unbounded_or_unknown_parameters(self) -> None:
        invalid_parameters = (
            {},
            {"checkpoint_ids": []},
            {"checkpoint_ids": "P01"},
            {"checkpoint_ids": ["P00"]},
            {"checkpoint_ids": ["P25"]},
            {"checkpoint_ids": ["P01", "P01"]},
            {"checkpoint_ids": ["P01"] * 25},
            {"checkpoint_ids": ["P01"], "path": "/bin/sh"},
            {"checkpoint_ids": ["P01"], "argv": ["id"]},
        )
        runner = AsyncMock()
        with patch.object(diagnostics, "_guest_exec", runner):
            for parameters in invalid_parameters:
                with self.subTest(parameters=parameters):
                    with self.assertRaises(Exception) as raised:
                        await diagnostics._collect(
                            diagnostics.DiagnosticTool.CHECK_COURSE_PROGRESS,
                            "lab-001",
                            parameters,
                        )
                    self.assertEqual(
                        getattr(raised.exception, "status_code", None), 422
                    )
        runner.assert_not_awaited()

    async def test_course_progress_rejects_invalid_guest_protocol_without_raw_output(
        self,
    ) -> None:
        valid = progress_payload(("P01",))
        course_mismatch = json.loads(json.dumps(valid))
        course_mismatch["observations"][0]["course_id"] = "other-course"
        id_mismatch = json.loads(json.dumps(valid))
        id_mismatch["observations"][0]["checkpoint_id"] = "P02"
        invalid_state = json.loads(json.dumps(valid))
        invalid_state["observations"][0]["state"] = ["passed"]
        invalid_evidence_type = json.loads(json.dumps(valid))
        invalid_evidence_type["observations"][0]["evidence_type"] = "raw output"
        redaction_expands_summary = json.loads(json.dumps(valid))
        redaction_expands_summary["observations"][0]["summary"] = (
            "x" * 500 + " token=a"
        )
        too_many_fact_entries = json.loads(json.dumps(valid))
        too_many_fact_entries["observations"][0]["facts"] = {
            "left": {f"a{index}": index for index in range(64)},
            "right": {f"b{index}": index for index in range(64)},
        }
        cases = (
            diagnostics.GuestResult(0, "not-json-secret", ""),
            diagnostics.GuestResult(0, json.dumps(valid), "", truncated=True),
            diagnostics.GuestResult(2, "raw-stdout-secret", "raw-stderr-secret"),
            diagnostics.GuestResult(0, json.dumps(course_mismatch), ""),
            diagnostics.GuestResult(0, json.dumps(id_mismatch), ""),
            diagnostics.GuestResult(0, json.dumps(invalid_state), ""),
            diagnostics.GuestResult(0, json.dumps(invalid_evidence_type), ""),
            diagnostics.GuestResult(0, json.dumps(redaction_expands_summary), ""),
            diagnostics.GuestResult(0, json.dumps(too_many_fact_entries), ""),
        )
        for guest_result in cases:
            with self.subTest(guest_result=guest_result):
                with (
                    patch.object(
                        diagnostics,
                        "_guest_exec",
                        AsyncMock(return_value=guest_result),
                    ),
                    self.assertRaises(Exception) as raised,
                ):
                    await diagnostics._collect(
                        diagnostics.DiagnosticTool.CHECK_COURSE_PROGRESS,
                        "lab-001",
                        {"checkpoint_ids": ["P01"]},
                    )
                self.assertEqual(getattr(raised.exception, "status_code", None), 502)
                detail = str(getattr(raised.exception, "detail", ""))
                self.assertNotIn("secret", detail)


if __name__ == "__main__":
    unittest.main()
