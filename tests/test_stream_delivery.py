import asyncio
import json
import unittest
from unittest.mock import patch

from aivirteach_agent.app import create_app
from aivirteach_agent.providers import FakeProvider, ProviderStreamDone, ProviderTextDelta
from test_agent import FakeGateway, request_payload, settings


class StreamDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delta_is_sent_before_model_finishes_and_result_is_available(self):
        release_model = asyncio.Event()
        delta_delivered = asyncio.Event()
        body_parts = []
        provider = FakeProvider()

        async def controlled_stream(**kwargs):
            yield ProviderTextDelta(text="请检查")
            await release_model.wait()
            yield ProviderTextDelta(text=" Docker。")
            yield ProviderStreamDone(finish_reason="stop")

        async def send(message):
            if message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))
                if b"event: assistant_delta" in message.get("body", b""):
                    delta_delivered.set()

        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": json.dumps(request_payload()).encode(),
                    "more_body": False,
                }
            await asyncio.Event().wait()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/v1/agent/diagnose/stream", "raw_path": b"/v1/agent/diagnose/stream",
            "query_string": b"", "root_path": "",
            "headers": [(b"authorization", b"Bearer agent-token"), (b"content-type", b"application/json")],
            "server": ("test", 80), "client": ("test", 1234),
        }
        app = create_app(settings=settings(), provider=provider, gateway=FakeGateway())
        with patch.object(provider, "stream_complete", controlled_stream):
            task = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(delta_delivered.wait(), timeout=1)
                self.assertFalse(task.done(), "Response finished before the model was released")
                self.assertFalse(release_model.is_set())
                self.assertNotIn(b"event: result", b"".join(body_parts))
                release_model.set()
                await asyncio.wait_for(task, timeout=1)
                output = b"".join(body_parts).decode()
                self.assertLess(output.index("event: assistant_delta"), output.index("event: result"))
                self.assertIn("event: done", output)
            finally:
                release_model.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
