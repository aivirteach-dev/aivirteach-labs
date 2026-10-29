import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from aivirteach_agent.app import create_app
from aivirteach_agent.models import DiagnoseRequest
from aivirteach_agent.orchestrator import AgentOrchestrator, _parse_assessment
from aivirteach_agent.providers import FakeProvider, ProviderTurn
from test_agent import FakeGateway, _parse_sse, request_payload, settings
from test_scheme_b import ASSESSMENT


class AssessmentParsingTests(unittest.TestCase):
    def test_accepts_assessment_after_balanced_preamble(self):
        for prefix in ['Context: {"status":"ready"}. ', 'Examples: [{"diagnosis": {}}]. ', '```json\n']:
            raw = prefix + json.dumps(ASSESSMENT) + ('\n```' if prefix.startswith('```') else '')
            self.assertEqual(_parse_assessment(raw).diagnosis.summary, ASSESSMENT['diagnosis']['summary'])

    def test_rejects_legacy_answer_and_ambiguous_or_incomplete_assessments(self):
        raw = json.dumps(ASSESSMENT)
        for text in [json.dumps({**ASSESSMENT, 'answer': 'must not become a reply'}), raw + raw,
                     '[' + raw, '[' + raw + ']', '{"diagnosis":', '{"debug":' + raw,
                     json.dumps({'diagnosis': {'summary': ' '}}), 'provider internal detail']:
            with self.subTest(text=text[:70]), self.assertRaises(ValueError):
                _parse_assessment(text)

    def test_escaping_preserves_assessment_content(self):
        for fragment in ['{', '}', '[', ']', '"', '\\', '\n', '中文', '```bash']:
            assessment = {**ASSESSMENT, 'diagnosis': {**ASSESSMENT['diagnosis'], 'summary': 'Check ' + fragment}}
            self.assertEqual(_parse_assessment(json.dumps(assessment)).diagnosis.summary, 'Check ' + fragment)


class AssessmentFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_assessment_never_reaches_renderer_or_leaks_raw_text(self):
        for raw in ['{"diagnosis":', json.dumps({'answer': 'do unsafe things', 'diagnosis': []}),
                    json.dumps({**ASSESSMENT, 'evidence_ids': ['invented']})]:
            provider = FakeProvider([ProviderTurn(text=raw)])
            provider.stream_complete = AsyncMock()
            response = await AgentOrchestrator(settings=settings(), provider=provider, gateway=FakeGateway()).diagnose(
                DiagnoseRequest.model_validate(request_payload()))
            self.assertEqual(response.answer, '暂时无法生成完整回答，请稍后重试。')
            self.assertEqual(response.status, 'partial')
            provider.stream_complete.assert_not_called()

    async def test_empty_render_uses_assessment_summary_without_another_model_call(self):
        provider = FakeProvider()
        async def empty_stream(**kwargs):
            if False:
                yield
        provider.stream_complete = empty_stream
        events = []
        async def capture(event, data): events.append(event)
        with patch.object(provider, 'complete', AsyncMock(return_value=ProviderTurn(text=json.dumps(ASSESSMENT)))) as complete:
            response = await AgentOrchestrator(settings=settings(), provider=provider, gateway=FakeGateway()).diagnose_stream(
                DiagnoseRequest.model_validate(request_payload()), on_event=capture)
        self.assertEqual(complete.await_count, 1)
        self.assertIn(ASSESSMENT['diagnosis']['summary'], response.answer)
        self.assertIn('Check service logs.', response.answer)
        self.assertIn('MODEL_STREAM_EMPTY', response.limitations)
        self.assertIn('answer_failed', events)
        self.assertEqual(response.status, 'partial')

    async def test_http_and_sse_reject_invalid_assessments_consistently(self):
        for language in ['zh-CN', 'en']:
            for path in ['/v1/agent/diagnose', '/v1/agent/diagnose/stream']:
                provider = FakeProvider([ProviderTurn(text='{"answer":"private draft","diagnosis":[]}')])
                app = create_app(settings=settings(), provider=provider, gateway=FakeGateway())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post(path, headers={'Authorization':'Bearer agent-token'},
                                                 json={**request_payload(), 'response_language':language})
                self.assertEqual(response.status_code, 200)
                if path.endswith('/stream'):
                    events = _parse_sse(response.text)
                    body = next(data['response'] for event,data in events if event == 'result')
                    self.assertFalse(any(event=='assistant_delta' for event,data in events))
                else:
                    body=response.json()
                self.assertEqual(body['status'],'partial')
                self.assertNotIn('private draft', body['answer'])
                self.assertEqual(body['answer'], '暂时无法生成完整回答，请稍后重试。' if language=='zh-CN' else "I couldn't generate a complete answer. Please try again.")
