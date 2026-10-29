import asyncio
import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ["AIVIRTEACH_API_TOKEN"] = "test-token"
os.environ["AIVIRTEACH_SESSION_TOKEN"] = "session-test-token"
os.environ["AIVIRTEACH_GUACAMOLE_JSON_SECRET"] = (
    "00112233445566778899aabbccddeeff"
)

import httpx
from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import service


GUACAMOLE_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


def decrypt_guacamole_ticket(data: str) -> dict[str, object]:
    decryptor = Cipher(
        algorithms.AES(GUACAMOLE_KEY), modes.CBC(bytes(16))
    ).decryptor()
    padded = decryptor.update(base64.b64decode(data)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    signed = unpadder.update(padded) + unpadder.finalize()
    signature, plaintext = signed[:32], signed[32:]
    verifier = crypto_hmac.HMAC(GUACAMOLE_KEY, hashes.SHA256())
    verifier.update(plaintext)
    verifier.verify(signature)
    return json.loads(plaintext)


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        service._lab_locks.clear()
        os.environ["AIVIRTEACH_API_TOKEN"] = "test-token"
        os.environ["AIVIRTEACH_SESSION_TOKEN"] = "session-test-token"
        os.environ["AIVIRTEACH_GUACAMOLE_JSON_SECRET"] = (
            "00112233445566778899aabbccddeeff"
        )
        os.environ["AIVIRTEACH_BROWSER_SESSION_TTL"] = "60"
        os.environ["AIVIRTEACH_RDP_ALLOWED_CIDRS"] = "192.168.122.0/24"
        transport = httpx.ASGITransport(app=service.app)
        self.client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        )
        self.auth = {"Authorization": "Bearer test-token"}
        self.session_auth = {
            "Authorization": "Bearer session-test-token"
        }

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_health_does_not_require_authentication(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    async def test_runner_does_not_forward_service_secrets(self) -> None:
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"ok", b"")
        spawn = AsyncMock(return_value=process)
        with patch.object(service.asyncio, "create_subprocess_exec", spawn):
            result = await service.run_script(["/bin/true"])

        self.assertEqual(result, "ok")
        child_env = spawn.await_args.kwargs["env"]
        for name in service.SENSITIVE_SUBPROCESS_ENV:
            self.assertNotIn(name, child_env)
        self.assertEqual(child_env["AIVIRTEACH_NONINTERACTIVE"], "true")

    async def test_protected_endpoint_rejects_missing_token(self) -> None:
        response = await self.client.get("/v1/vms/lab-001/status")
        self.assertEqual(response.status_code, 401)

    async def test_admin_api_is_disabled_without_admin_token(self) -> None:
        with patch.dict(os.environ, {"AIVIRTEACH_API_TOKEN": ""}):
            response = await self.client.get(
                "/v1/vms/lab-001/status",
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 503)

    async def test_status_is_parsed(self) -> None:
        output = "Name: lab-001\nState: running\nCPU(s): 2"
        with patch.object(service, "run_script", AsyncMock(return_value=output)):
            response = await self.client.get(
                "/v1/vms/lab-001/status", headers=self.auth
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["State"], "running")

    async def test_list_vms_returns_names_uuids_and_states(self) -> None:
        output = (
            "lab-001\t26a6db7e-1ea7-4de2-9ca3-cf58edbab809\trunning\n"
            "lab-002\t50eb674a-91b2-4f26-93a9-c11efbaa3327\tshut off"
        )
        runner = AsyncMock(return_value=output)
        with patch.object(service, "run_script", runner):
            response = await self.client.get("/v1/vms", headers=self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "count": 2,
                "vms": [
                    {
                        "lab_id": "lab-001",
                        "vm_instance_id": "26a6db7e-1ea7-4de2-9ca3-cf58edbab809",
                        "state": "running",
                    },
                    {
                        "lab_id": "lab-002",
                        "vm_instance_id": "50eb674a-91b2-4f26-93a9-c11efbaa3327",
                        "state": "shut off",
                    },
                ],
            },
        )
        runner.assert_awaited_once_with([service.VM_CONTROL_SCRIPT, "list"])

    async def test_list_vms_rejects_missing_token(self) -> None:
        response = await self.client.get("/v1/vms")
        self.assertEqual(response.status_code, 401)

    async def test_list_vms_rejects_malformed_inventory(self) -> None:
        with patch.object(
            service,
            "run_script",
            AsyncMock(return_value="lab-001\tnot-a-uuid\trunning"),
        ):
            response = await self.client.get("/v1/vms", headers=self.auth)

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("not-a-uuid", response.text)

    async def test_ip_response(self) -> None:
        with patch.object(
            service, "run_script", AsyncMock(return_value="192.168.122.210")
        ):
            response = await self.client.get(
                "/v1/vms/lab-001/ip", headers=self.auth
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"lab_id": "lab-001", "ip_address": "192.168.122.210"},
        )

    async def test_create_uses_target_repository_scripts(self) -> None:
        runner = AsyncMock(
            return_value=(
                "Learner VM created.\n"
                "VM: lab-002\n"
                "VM instance ID: 26a6db7e-1ea7-4de2-9ca3-cf58edbab809\n"
                "Username: learner\n"
                "RDP password: generated-secret"
            )
        )
        with patch.object(service, "run_script", runner):
            response = await self.client.post(
                "/v1/vms",
                headers=self.auth,
                json={"lab_id": "lab-002", "memory_mb": 2048, "vcpus": 1},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["rdp_password"], "generated-secret")
        self.assertEqual(
            response.json()["vm_instance_id"],
            "26a6db7e-1ea7-4de2-9ca3-cf58edbab809",
        )
        argv = runner.await_args.args[0]
        self.assertEqual(argv[0], service.CREATE_VM_SCRIPT)
        self.assertEqual(
            [str(item) for item in argv[1:]],
            ["lab-002", "--memory", "2048", "--vcpus", "1"],
        )

    async def test_invalid_lab_id_is_rejected_before_script_execution(self) -> None:
        runner = AsyncMock()
        with patch.object(service, "run_script", runner):
            response = await self.client.get(
                "/v1/vms/not valid/ip",
                headers=self.auth,
            )

        self.assertEqual(response.status_code, 422)
        runner.assert_not_awaited()

    async def test_create_rejects_a_missing_vm_instance_id(self) -> None:
        runner = AsyncMock(
            return_value=(
                "Learner VM created.\n"
                "VM: lab-002\n"
                "Username: learner\n"
                "RDP password: generated-secret"
            )
        )
        with patch.object(service, "run_script", runner):
            response = await self.client.post(
                "/v1/vms",
                headers=self.auth,
                json={"lab_id": "lab-002"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("generated-secret", response.text)

    async def test_delete_requires_explicit_confirmation(self) -> None:
        runner = AsyncMock()
        with patch.object(service, "run_script", runner):
            response = await self.client.delete(
                "/v1/vms/lab-001", headers=self.auth
            )

        self.assertEqual(response.status_code, 400)
        runner.assert_not_awaited()

    async def test_browser_session_rejects_admin_token(self) -> None:
        response = await self.client.post(
            "/v1/vms/lab-001/browser-sessions",
            headers=self.auth,
            json={"subject": "learner_advanced"},
        )
        self.assertEqual(response.status_code, 401)

    async def test_browser_session_rejects_identical_admin_and_session_tokens(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "AIVIRTEACH_API_TOKEN": "same-token",
                "AIVIRTEACH_SESSION_TOKEN": "same-token",
            },
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers={"Authorization": "Bearer same-token"},
                json={"subject": "learner_advanced"},
            )
        self.assertEqual(response.status_code, 503)

    async def test_browser_session_serializes_vm_start(self) -> None:
        vm = {"state": "shut off", "starts": 0}

        async def runner(argv, **_kwargs):
            action = str(argv[1])
            if action == "status":
                observed_state = vm["state"]
                await asyncio.sleep(0.01)
                return f"Name: lab-001\nState: {observed_state}"
            if action == "start":
                vm["starts"] += 1
                vm["state"] = "running"
                return "Domain 'lab-001' started"
            if action == "ip":
                return "192.168.122.210"
            raise AssertionError(f"Unexpected action: {action}")

        with (
            patch.object(service, "run_script", runner),
            patch.object(service, "_rdp_ready", AsyncMock(return_value=False)),
        ):
            responses = await asyncio.gather(
                self.client.post(
                    "/v1/vms/lab-001/browser-sessions",
                    headers=self.session_auth,
                    json={"subject": "learner_one"},
                ),
                self.client.post(
                    "/v1/vms/lab-001/browser-sessions",
                    headers=self.session_auth,
                    json={"subject": "learner_two"},
                ),
            )

        self.assertEqual([item.status_code for item in responses], [200, 200])
        self.assertEqual(
            [item.json()["state"] for item in responses],
            ["starting", "starting"],
        )
        self.assertEqual(vm["starts"], 1)

    async def test_browser_session_starts_stopped_vm(self) -> None:
        runner = AsyncMock(
            side_effect=[
                "Name: lab-001\nState: shut off",
                "Domain 'lab-001' started",
            ]
        )
        with patch.object(service, "run_script", runner):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "starting")
        self.assertEqual(runner.await_count, 2)

    async def test_browser_session_returns_opaque_ticket(self) -> None:
        runner = AsyncMock(
            side_effect=[
                "Name: lab-001\nState: running",
                "192.168.122.210",
                "lab_id=lab-001\nusername=learner\npassword=secret\nrdp_port=3389",
            ]
        )
        with (
            patch.object(service, "run_script", runner),
            patch.object(service, "_rdp_ready", AsyncMock(return_value=True)),
            patch.object(service.time, "time", return_value=1_700_000_000),
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["lab_id"], "lab-001")
        self.assertEqual(body["state"], "ready")
        self.assertIsInstance(body["data"], str)
        self.assertEqual(body["expires_at"], 1_700_000_060_000)
        self.assertNotIn("password", body)
        self.assertNotIn("ip_address", body)
        self.assertEqual(response.headers["cache-control"], "no-store")

        ticket = decrypt_guacamole_ticket(body["data"])
        self.assertEqual(ticket["username"], "learner_advanced")
        self.assertEqual(ticket["expires"], 1_700_000_060_000)
        connection = ticket["connections"]["lab-001"]
        self.assertEqual(connection["protocol"], "rdp")
        self.assertEqual(
            connection["parameters"],
            {
                "hostname": "192.168.122.210",
                "port": "3389",
                "username": "learner",
                "password": "secret",
                "security": "any",
                "ignore-cert": "true",
                "resize-method": "display-update",
                "enable-wallpaper": "true",
            },
        )

    async def test_browser_session_rejects_invalid_rdp_port(self) -> None:
        runner = AsyncMock(
            side_effect=[
                "Name: lab-001\nState: running",
                "192.168.122.210",
                "lab_id=lab-001\nusername=learner\npassword=secret\nrdp_port=invalid",
            ]
        )
        with (
            patch.object(service, "run_script", runner),
            patch.object(service, "_rdp_ready", AsyncMock(return_value=True)),
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )
        self.assertEqual(response.status_code, 503)

    async def test_browser_session_rejects_ip_outside_lab_network(self) -> None:
        runner = AsyncMock(
            side_effect=[
                "Name: lab-001\nState: running",
                "127.0.0.1",
            ]
        )
        readiness = AsyncMock()
        with (
            patch.object(service, "run_script", runner),
            patch.object(service, "_rdp_ready", readiness),
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )
        self.assertEqual(response.status_code, 503)
        readiness.assert_not_awaited()

    async def test_browser_session_rejects_invalid_ttl(self) -> None:
        runner = AsyncMock()
        with (
            patch.dict(os.environ, {"AIVIRTEACH_BROWSER_SESSION_TTL": "invalid"}),
            patch.object(service, "run_script", runner),
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )
        self.assertEqual(response.status_code, 503)
        runner.assert_not_awaited()

    async def test_browser_session_rejects_invalid_json_secret_before_vm_start(
        self,
    ) -> None:
        runner = AsyncMock()
        with (
            patch.dict(
                os.environ,
                {"AIVIRTEACH_GUACAMOLE_JSON_SECRET": "not-a-128-bit-key"},
            ),
            patch.object(service, "run_script", runner),
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )
        self.assertEqual(response.status_code, 503)
        runner.assert_not_awaited()

    async def test_browser_session_rejects_invalid_cidr_before_vm_start(
        self,
    ) -> None:
        runner = AsyncMock()
        with (
            patch.dict(os.environ, {"AIVIRTEACH_RDP_ALLOWED_CIDRS": "invalid"}),
            patch.object(service, "run_script", runner),
        ):
            response = await self.client.post(
                "/v1/vms/lab-001/browser-sessions",
                headers=self.session_auth,
                json={"subject": "learner_advanced"},
            )
        self.assertEqual(response.status_code, 503)
        runner.assert_not_awaited()


class ErrorStatusTestCase(unittest.TestCase):
    """调用方（aivirteach-server）只在 404 + "VM not found" 时认定 VM 不存在，
    所以 libvirt 本身不可用必须映射成 503，不能落进 404。"""

    def test_vm_not_found_is_404(self) -> None:
        self.assertEqual(service._error_status("ERROR: VM not found"), 404)

    def test_libvirt_unavailable_is_503(self) -> None:
        output = (
            "ERROR: libvirt unavailable while checking 'lab-1': "
            "error: failed to connect to the hypervisor"
        )
        self.assertEqual(service._error_status(output), 503)


if __name__ == "__main__":
    unittest.main()
