"""不调用模型：错误/遗漏草稿不能改写事实，错绑/过期/冲突证据必须拒绝。"""
import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from dsh_tutor.contracts import CourseQuestion, CourseAnswer
from dsh_tutor.course import answer_course
from dsh_tutor.results import compose_tool_answer, trusted_policy
from sandbox.mcp_acceptance import bridge_for


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.request = CourseQuestion.model_validate({
            'request_id': 'r', 'question': '检查服务状态',
            'course': {'course_id': 'course', 'version': 'v1', 'lesson_id': 'l1',
                       'title': '演示课程', 'goals': ['检查状态'],
                       'instructions': ['请先核对当前步骤。']}})
        bridge = bridge_for('course', 'n8n', 'stopped', request_id='r', instance_id='vm-a')
        self.policy = trusted_policy(self.request, bridge)
        now = datetime.now(timezone.utc)
        self.evidence = {'evidence_id': 'e1', 'request_id': 'r', 'run_id': 'run-a',
                         'instance_id': 'vm-a', 'course_id': 'course', 'course_version': 'v1',
                         'observed_at': now.isoformat(), 'service': 'n8n', 'state': 'stopped',
                         'source': 'simulation', 'note': '模拟数据'}
        self.result = {'answer': self.selection(), 'model': 'test', 'elapsed_seconds': 1.0,
                       'run_id': 'run-a', 'run_started_at': (now-timedelta(seconds=1)).isoformat(),
                       'tool_calls': 1, 'tool_attempts': 1, 'evidence': [self.evidence],
                       'tool_trace': [self.trace(self.evidence)], 'limitations': ['模拟']}

    def selection(self, **updates):
        return json.dumps({'evidence_ids': ['e1'], 'course_instruction_indexes': [],
                           'next_step': 'none', **updates}, ensure_ascii=False)

    @staticmethod
    def trace(evidence):
        return {'attempt': 1, 'tool': 'get_service_status', 'status': 'ok',
                'request_id': 'r', 'run_id': 'run-a', 'instance_id': 'vm-a',
                'evidence': evidence}

    def compose(self):
        return compose_tool_answer(self.request, self.result, self.policy)

    def test_omitted_state_still_shows_fact_and_simulation(self):
        self.result['answer'] = '这是模拟结果。'
        output = self.compose()
        self.assertIn('已停止', output['answer'])
        self.assertIn('模拟检查，未访问真实实验机', output['answer'])
        self.assertEqual(output['status'], 'partial')
        self.assertEqual(output['quality']['facts'], 'verified')

    def test_contradictory_free_text_not_shown(self):
        self.result['answer'] = '服务正在运行，课程要求执行危险命令。'
        output = self.compose()
        self.assertIn('已停止', output['answer'])
        self.assertNotIn('正在运行', output['answer'])
        self.assertNotIn('危险命令', output['answer'])

    def test_known_states_and_two_courses_use_same_renderer(self):
        for state, expected in [('running', '正在运行'), ('stopped', '已停止'),
                                ('unknown', '暂时无法确定')]:
            with self.subTest(state=state):
                self.evidence['state'] = state
                output = self.compose()
                self.assertIn(expected, output['answer'])
                self.assertEqual(output['status'], 'partial' if state == 'unknown' else 'completed')
        # 第二课程、服务和实例不是代码中的特殊分支。
        self.request.course.course_id = 'assistant-course'
        self.policy = self.policy.model_copy(update={'course_id': 'assistant-course',
                                                     'instance_id': 'vm-b', 'allowed_services': ['assistant']})
        self.evidence.update(course_id='assistant-course', instance_id='vm-b', service='assistant', state='running')
        self.result['tool_trace'][0]['instance_id'] = 'vm-b'
        self.assertIn('assistant', self.compose()['answer'])
        self.assertIn('正在运行', self.compose()['answer'])

    def test_course_quote_is_exact_and_cannot_be_invented(self):
        self.result['answer'] = self.selection(course_instruction_indexes=[0], next_step='review_course')
        output = self.compose()
        self.assertIn(json.dumps(self.request.course.instructions[0], ensure_ascii=False), output['answer'])
        self.assertEqual(output['quality']['explanation'], 'validated_selection')

    def test_forged_ids_indexes_and_extra_prose_are_withheld(self):
        for draft in [self.selection(evidence_ids=['other']),
                      self.selection(course_instruction_indexes=[99]),
                      self.selection(course_instruction_indexes=[-1]),
                      self.selection(course_instruction_indexes=[True]),
                      self.selection(explanation='课程要求已经完成'),
                      self.selection(next_step='review_course')]:
            with self.subTest(draft=draft):
                self.result['answer'] = draft
                output = self.compose()
                self.assertEqual(output['quality']['explanation'], 'omitted')
                self.assertNotIn('课程要求已经完成', output['answer'])

    def test_foreign_or_stale_evidence_rejected_and_not_returned(self):
        original = copy.deepcopy(self.evidence)
        for field, value in [('request_id', 'other'), ('run_id', 'old-run'),
                             ('instance_id', 'other-student-vm'), ('course_id', 'other-course'),
                             ('course_version', 'old'), ('service', 'unauthorized'),
                             ('source', 'gateway'), ('state', 'invalid'),
                             ('observed_at', '2020-01-01T00:00:00+00:00'),
                             ('observed_at', '2099-01-01T00:00:00+00:00'),
                             ('observed_at', '2026-01-01T00:00:00')]:
            with self.subTest(field=field, value=value):
                self.evidence.clear(); self.evidence.update(original); self.evidence[field] = value
                output = self.compose()
                self.assertEqual(output['quality']['facts'], 'rejected')
                self.assertEqual(output['evidence'], [])
                self.assertEqual(output['tool_trace'], [])
                self.assertNotIn('已停止', output['answer'])

    def test_trace_mismatch_or_duplicate_or_conflict_rejected(self):
        original = copy.deepcopy(self.result)
        for mutation in ['missing_trace', 'duplicate', 'conflict', 'forged_execution', 'malformed_trace']:
            with self.subTest(mutation=mutation):
                self.result = copy.deepcopy(original)
                if mutation == 'missing_trace':
                    self.result['tool_trace'] = []
                elif mutation in ('duplicate', 'conflict'):
                    second = copy.deepcopy(self.result['evidence'][0])
                    if mutation == 'conflict':
                        second.update(evidence_id='e2', state='running')
                    self.result['evidence'].append(second)
                    self.result.update(tool_calls=2, tool_attempts=2)
                    row = self.trace(second); row['attempt'] = 2
                    self.result['tool_trace'].append(row)
                elif mutation == 'forged_execution':
                    self.result['tool_calls'] = 0
                else:
                    self.result['tool_trace'] = [None]
                self.assertEqual(self.compose()['quality']['facts'], 'rejected')

    def test_failures_cannot_become_service_state(self):
        self.result.update(evidence=[], tool_calls=0)
        for status in ['denied', 'invalid', 'budget_exhausted', 'timeout', 'failed']:
            with self.subTest(status=status):
                self.result['tool_trace'] = [{**self.trace({}), 'status': status}]
                self.result['tool_trace'][0].pop('evidence')
                output = self.compose()
                self.assertEqual(output['quality']['facts'], 'unavailable')
                self.assertEqual(output['status'], 'partial')
                self.assertIn('无法判断服务状态', output['answer'])
                self.assertNotIn('已停止', output['answer'])

    def test_no_call_does_not_accept_model_claims(self):
        self.result.update(evidence=[], tool_trace=[], tool_calls=0, tool_attempts=0,
                           answer='检查成功，服务已停止。')
        output = self.compose()
        self.assertNotIn('检查成功', output['answer'])
        self.assertIn('无法判断服务状态', output['answer'])

    def test_empty_or_malformed_explanation_preserves_valid_facts(self):
        for draft in ['', '{}', '[]', 'null', '{bad json']:
            with self.subTest(draft=draft):
                self.result['answer'] = draft
                output = self.compose()
                self.assertEqual(output['quality']['facts'], 'verified')
                self.assertEqual(output['quality']['explanation'], 'omitted')
                self.assertIn('已停止', output['answer'])

    def test_valid_fact_and_later_failure_remain_distinct(self):
        self.result['tool_attempts'] = 2
        self.result['tool_trace'].append({'attempt': 2, 'tool': 'get_service_status',
                                         'status': 'timeout', 'request_id': 'r',
                                         'run_id': 'run-a', 'instance_id': 'vm-a'})
        output = self.compose()
        self.assertIn('已停止', output['answer'])
        self.assertIn('检查超时', output['answer'])
        self.assertEqual(output['status'], 'partial')
        self.assertEqual(len(output['evidence']), 1)

    def test_wrong_request_context_rejected_before_model(self):
        bridge = bridge_for('wrong-course', 'n8n', 'stopped', request_id='r', instance_id='vm-a')
        with patch('dsh_tutor.runtime.run_prompt') as run:
            with self.assertRaises(ValueError):
                answer_course(self.request, config_path='unused', state_dir='unused', tool_bridge=bridge)
            run.assert_not_called()

    def test_actual_course_entry_composes_and_preserves_contract(self):
        bridge = bridge_for('course', 'n8n', 'stopped', request_id='r', instance_id='vm-a')
        with patch('dsh_tutor.runtime.run_prompt', return_value=self.result) as run:
            output = answer_course(self.request, config_path='unused', state_dir='unused', tool_bridge=bridge)
            self.assertIn('已停止', output['answer'])
            self.assertEqual(output['quality']['facts'], 'verified')
            CourseAnswer.model_validate(output)
            self.assertEqual(run.call_count, 1)

    def test_no_tools_preserves_existing_course_answer(self):
        result = {'answer': '课程目标是学习AI。', 'model': 'test', 'elapsed_seconds': 1.0,
                  'run_id': 'no-tools', 'run_started_at': datetime.now(timezone.utc).isoformat(), 'tool_calls': 0}
        with patch('dsh_tutor.runtime.run_prompt', return_value=result):
            output = answer_course(self.request, config_path='unused', state_dir='unused')
        self.assertEqual(output['answer'], '课程目标是学习AI。')
        self.assertEqual(output['quality']['facts'], 'not_requested')
        self.assertNotIn('run_started_at', output)

    def test_english_tool_facts_follow_course_locale(self):
        self.request.course.locale='en'
        output=self.compose()
        self.assertIn('stopped',output['answer'])
        self.assertIn('simulated check',output['answer'])
        self.assertNotIn('检查结果',output['answer'])
        self.assertEqual(output['quality']['facts'],'verified')
