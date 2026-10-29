import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx


os.environ.setdefault("AIVIRTEACH_API_TOKEN", "test-token")
os.environ.setdefault("AIVIRTEACH_AGENT_TOKEN", "agent-token")
os.environ.setdefault("AIVIRTEACH_DIAGNOSTIC_TOKEN", "diagnostic-token")

import agent_service
import docs_service
import gateway_service
import service
from openapi_aggregator import ServiceSpec, merge_documents


class UnifiedOpenApiTests(unittest.TestCase):
    def setUp(self) -> None:
        specs = [
            ServiceSpec("vm_manager", "VM Manager", "http://vm.test:8760", "VM"),
            ServiceSpec(
                "diagnostic_gateway",
                "Diagnostic Gateway",
                "http://diagnostics.test:8765",
                "Diagnostics",
            ),
            ServiceSpec("agent", "Agent Service", "http://agent.test:8770", "Agent"),
        ]
        documents = [
            service.app.openapi(),
            gateway_service.app.openapi(),
            agent_service.app.openapi(),
        ]
        self.schema = merge_documents(list(zip(specs, documents, strict=True)))

    def test_docs_include_all_three_service_apis(self) -> None:
        paths = self.schema["paths"]
        self.assertIn("/v1/vms", paths)
        self.assertIn("get", paths["/v1/vms"])
        self.assertIn("post", paths["/v1/vms"])
        self.assertIn("/v1/vms/{lab_id}/status", paths)
        self.assertIn("/v1/diagnostics/{lab_id}/tools/{tool}", paths)
        self.assertIn("/v1/agent/diagnose", paths)
        self.assertIn("/v1/agent/diagnose/stream", paths)
        self.assertNotIn("/health", paths)
        self.assertNotIn("/ready", paths)

    def test_operations_publish_their_real_service_addresses(self) -> None:
        paths = self.schema["paths"]
        self.assertEqual(
            paths["/v1/vms/{lab_id}/status"]["get"]["servers"][0]["url"],
            "http://vm.test:8760",
        )
        self.assertEqual(
            paths["/v1/diagnostics/{lab_id}/tools/{tool}"]["post"]["servers"][0]["url"],
            "http://diagnostics.test:8765",
        )
        self.assertEqual(
            paths["/v1/agent/diagnose"]["post"]["servers"][0]["url"],
            "http://agent.test:8770",
        )

    def test_docs_define_three_distinct_bearer_schemes(self) -> None:
        schemes = self.schema["components"]["securitySchemes"]
        self.assertIn("AdminBearer", schemes)
        self.assertIn("DiagnosticBearer", schemes)
        self.assertIn("AgentBearer", schemes)

    def test_schema_names_are_namespaced_by_service(self) -> None:
        schemas = self.schema["components"]["schemas"]
        self.assertTrue(any(name.startswith("vm_manager_") for name in schemas))
        self.assertTrue(
            any(name.startswith("diagnostic_gateway_") for name in schemas)
        )
        self.assertTrue(any(name.startswith("agent_") for name in schemas))


class DocsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=docs_service.app),
            base_url="http://docs.test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_swagger_assets_are_self_hosted(self) -> None:
        response = await self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/docs-static/swagger-ui-bundle.js", response.text)
        self.assertIn("/docs-static/swagger-ui.css", response.text)
        self.assertNotIn("cdn.jsdelivr.net", response.text)

    async def test_openapi_endpoint_returns_aggregated_document(self) -> None:
        document = {
            "openapi": "3.1.0",
            "info": {"title": "AIVirTeach Labs API", "version": "2.0.0"},
            "paths": {"/v1/example": {"get": {}}},
        }
        with patch.object(
            docs_service,
            "build_unified_document",
            AsyncMock(return_value=document),
        ):
            response = await self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), document)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_docs_service_does_not_mount_runtime_operations(self) -> None:
        response = await self.client.post("/v1/vms")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
