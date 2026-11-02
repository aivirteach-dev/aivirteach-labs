"""固定源码契约的兼容边界：保留教材字段，无法映射的数据显式拒绝。"""
from copy import deepcopy
from uuid import uuid4
import json,unittest
from dsh_tutor.platform_adapter import adapt_platform,platform_response
from dsh_tutor.service_contracts import ServiceError


def platform_request():
    return {'request_id':str(uuid4()),'lab_id':'demo-lab','question':'课程目标是什么？',
        'course':{'course_id':'course-a','version':1,'title':'虚构课程','summary':'课程摘要',
            'relevant_excerpts':[{'title':'摘录','content':'原始摘录内容'}]},
        'current_step':{'module_id':'module-a','lesson_id':'l1','sequence':1,'title':'步骤一','summary':'步骤摘要',
            'instructions':['操作一'],'expected_result':'期望结果','success_criteria':['标准一'],
            'common_failures':[{'code':'DEMO','symptoms':['虚构症状']}]}}


class AdapterTests(unittest.TestCase):
    def test_course_version_and_all_teaching_fields_are_preserved(self):
        raw=platform_request();result=adapt_platform(raw,lab_id='demo-lab')
        teaching=json.loads(result.course.instructions[0].split('：',1)[1])
        for key,value in raw['current_step'].items():self.assertEqual(teaching['current_step'][key],value)
        for key,value in raw['course'].items():self.assertEqual(teaching['course'][key],value)
        self.assertEqual(result.course.version,'1')
    def test_nonempty_unmapped_history_learner_state_and_scope_not_silently_discarded(self):
        for key,value in [('history',[{'role':'user','content':'历史'}]),('learner_state',{'goal':'目标'}),('diagnostic_scope',{'allowed_tools':['shell']})]:
            raw=platform_request();raw[key]=value
            with self.assertRaises(ServiceError) as exc:adapt_platform(raw,lab_id='demo-lab')
            self.assertEqual(exc.exception.code,'unsupported_dynamic_context')
    def test_wrong_lab_overlong_context_unknown_fields_and_nonuuid_rejected(self):
        raw=platform_request()
        with self.assertRaises(ServiceError):adapt_platform(raw,lab_id='other-lab')
        for change in [('request_id','wrong'),('question','x'*2001),('admin',True)]:
            item=deepcopy(raw);item[change[0]]=change[1]
            with self.assertRaises(ServiceError):adapt_platform(item,lab_id='demo-lab')
        raw['current_step']['instructions']=['x'*5100]
        with self.assertRaises(ServiceError) as exc:adapt_platform(raw,lab_id='demo-lab')
        self.assertEqual(exc.exception.code,'platform_context_too_large')
    def test_partial_projection_never_claims_platform_save_or_fabricates_token_count(self):
        view={'state':'succeeded','request_id':str(uuid4()),'result':{'answer':'课程回答','detail':None}}
        result=platform_response(view)
        self.assertEqual(result['status'],'partial');self.assertIsNone(result['usage']);self.assertEqual(result['evidence'],[])
        self.assertIn('平台仍须正式保存',result['limitations'][1])
        view['result']['detail']={'evidence':[{'source':'simulation'}]}
        with self.assertRaises(ServiceError):platform_response(view)
