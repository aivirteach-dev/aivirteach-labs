import json
import unittest
from unittest.mock import AsyncMock

from aivirteach_agent.models import DiagnoseRequest
from aivirteach_agent.orchestrator import AgentOrchestrator
from aivirteach_agent.providers import FakeProvider, ProviderStreamDone, ProviderTextDelta, ProviderTurn
from test_agent import FakeGateway, request_payload, settings

ASSESSMENT = {
    'diagnosis': {'summary': 'Docker may be stopped.', 'probable_causes': ['Service stopped'], 'confidence': 'low'},
    'course_alignment': {'expected': ['Service active'], 'observed': []},
    'evidence_ids': [], 'suggested_actions': [{'title': 'Inspect', 'detail': 'Check service logs.'}],
    'limitations': ['No live observation'],
}

class RecordingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.assessment_prompts = []
        self.answer_prompts = []

    async def complete(self, **kwargs):
        self.assessment_prompts.append(kwargs['messages'])
        return ProviderTurn(text=json.dumps(ASSESSMENT))

    async def stream_complete(self, **kwargs):
        self.answer_prompts.append(kwargs['messages'])
        yield ProviderTextDelta(text='Check service logs.')
        yield ProviderStreamDone(finish_reason='stop')

class SchemeBTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_transports_generate_a_learner_answer_once_from_assessment(self):
        for stream in [False, True]:
            provider = RecordingProvider()
            orchestrator = AgentOrchestrator(settings=settings(), provider=provider, gateway=FakeGateway())
            request = DiagnoseRequest.model_validate(request_payload())
            response = await (orchestrator.diagnose_stream(request, on_event=AsyncMock()) if stream else orchestrator.diagnose(request))
            self.assertEqual(response.answer, 'Check service logs.')
            self.assertEqual(len(provider.assessment_prompts), 1)
            self.assertEqual(len(provider.answer_prompts), 1)
            self.assertIn('Do not write a learner-facing answer', provider.assessment_prompts[0][0].content)
            context = json.loads(provider.answer_prompts[0][-1].content)
            self.assertNotIn('answer', context['validated_assessment'])
            self.assertEqual(context['validated_assessment']['diagnosis']['confidence'], 'low')
