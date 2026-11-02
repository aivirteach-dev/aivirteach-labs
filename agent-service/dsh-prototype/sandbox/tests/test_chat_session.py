"""自动历史问答的故障/隔离/预算/并发回归；替换推理传输，不访问Mac。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.storage import SQLiteChatStore,StorageError
from sandbox.tests.test_storage import scope


def raw_answer(prompt,**kwargs):
    return dict(answer='测试回答',model='fake',run_id=uuid4().hex,
                run_started_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=0.01)


def data(prompt):
    return json.loads(prompt.split('以下 JSON 是本轮课程与学生问题：\n')[1])


class ChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'chat.db'
        self.store=SQLiteChatStore.create(self.path)
        self.h=self.store.activate(scope())

    def tearDown(self):self.temp.cleanup()

    def request(self,rid='new',**course_updates):
        course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构课程',instructions=[])
        course.update(course_updates)
        return CourseQuestion(request_id=rid,course=course,question='请继续刚才的问题。')

    def ask(self,request=None,**kwargs):
        return answer_chat(request or self.request(),store=kwargs.pop('store',self.store),
            handle=kwargs.pop('handle',self.h),config_path='unused',state_dir=self.temp.name,**kwargs)

    def seed(self,rid='old',answer='OLD_ANSWER',answer_status='completed'):
        self.store.begin(self.h,rid,'OLD_QUESTION')
        self.store.finish(self.h,rid,answer=answer,answer_status=answer_status,run_id='old-run')

    def test_restored_history_reaches_real_course_prompt_and_final_answer_saved(self):
        self.seed()
        reopened=SQLiteChatStore(self.path)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            result=self.ask(store=reopened)
        self.assertEqual(result.state,'saved')
        history=data(run.call_args.args[0])['conversation']['history']
        self.assertEqual(history,[{'role':'user','content':'OLD_QUESTION'},{'role':'assistant','content':'OLD_ANSWER'}])
        self.assertEqual(result.result.context_usage.used_turns,1)
        row=self.store.read_page(self.h).turns[-1]
        self.assertEqual((row.answer,row.status,row.run_id),('测试回答','completed',result.run_id))

    def test_fresh_process_loads_history_without_manual_turn_input(self):
        self.seed(answer='PROCESS_CANARY')
        code="""
import sys,json
from unittest.mock import patch
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from sandbox.tests.test_chat_session import raw_answer,data
s=SQLiteChatStore(sys.argv[1]);h=s.current(student_id='student-a',learning_context_id='enrollment-a')
q=CourseQuestion(request_id='process-followup',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'),question='继续')
with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
 r=answer_chat(q,store=s,handle=h,config_path='unused',state_dir=sys.argv[2])
 assert r.state=='saved'
 assert data(run.call_args.args[0])['conversation']['history'][-1]['content']=='PROCESS_CANARY'
"""
        result=subprocess.run([sys.executable,'-c',code,str(self.path),self.temp.name],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_duplicate_completed_replays_without_model_or_history_read(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            first=self.ask()
            with patch.object(self.store,'read_page',side_effect=AssertionError('should not read')):
                second=self.ask()
        self.assertEqual(run.call_count,1)
        self.assertEqual(second.state,'replayed')
        self.assertEqual(first.answer,second.answer)
        self.assertIsNone(second.result)

    def test_concurrent_duplicates_only_one_claim_can_call_model(self):
        entered=Event();release=Event()
        def transport(prompt,**kwargs):
            entered.set();self.assertTrue(release.wait(5));return raw_answer(prompt,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(self.ask)
                self.assertTrue(entered.wait(5))
                second=pool.submit(self.ask).result(timeout=5)
                release.set();final=first.result(timeout=5)
        self.assertEqual(second.state,'in_progress')
        self.assertEqual(final.state,'saved')
        self.assertEqual(run.call_count,1)

    def test_pending_and_failed_retries_do_not_call_model(self):
        self.store.begin(self.h,'new',self.request().question)
        with patch('dsh_tutor.runtime.run_prompt') as run:
            self.assertEqual(self.ask().state,'in_progress')
            self.store.finish(self.h,'new',error_code='interrupted')
            self.assertEqual(self.ask().state,'failed')
            run.assert_not_called()

    def test_bad_course_binding_rejected_before_accept(self):
        with patch('dsh_tutor.runtime.run_prompt') as run:
            result=self.ask(self.request(version='wrong'))
        self.assertEqual(result.stage,'input');run.assert_not_called()
        self.assertEqual(self.store.read_page(self.h).turns,[])

    def test_other_student_rejected_without_loading_or_running(self):
        foreign=self.h.model_copy(update={'scope':self.h.scope.model_copy(update={'student_id':'other'})})
        with patch('dsh_tutor.runtime.run_prompt') as run:
            result=self.ask(handle=foreign)
        self.assertEqual(result.error_code,'scope_denied');run.assert_not_called()

    def test_changed_scope_starts_empty_and_old_handle_rejected(self):
        self.seed(answer='OLD_SCOPE_CANARY')
        new=self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            result=self.ask(handle=new)
            rejected=self.ask(self.request('other'),handle=self.h)
        self.assertEqual(result.state,'saved')
        self.assertEqual(data(run.call_args.args[0])['conversation']['history'],[])
        self.assertNotIn('OLD_SCOPE_CANARY',run.call_args.args[0])
        self.assertEqual(rejected.state,'scope_changed')
        self.assertEqual(run.call_count,1)

    def test_state_change_while_model_runs_withholds_old_answer(self):
        def transport(prompt,**kwargs):
            self.store.activate(scope(instance_generation='g2'),expected_revision=1)
            return raw_answer(prompt,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):result=self.ask()
        self.assertEqual(result.state,'scope_changed')
        self.assertIsNone(result.answer);self.assertIsNone(result.result)
        self.assertEqual(self.store.read_page(self.h).turns[-1].status,'cancelled')

    def test_history_failure_does_not_fall_back_to_empty_context(self):
        with patch.object(self.store,'read_page',side_effect=StorageError('unavailable')):
            with patch('dsh_tutor.runtime.run_prompt') as run:result=self.ask()
        self.assertEqual((result.stage,result.error_code),('history','history_unavailable'))
        run.assert_not_called()
        self.assertEqual(self.store.read_page(self.h).turns[0].status,'failed')

    def test_failure_to_record_failure_exposes_recovery_required(self):
        with patch.object(self.store,'read_page',side_effect=StorageError('busy')):
            with patch.object(self.store,'finish',side_effect=StorageError('busy')):
                with patch('dsh_tutor.runtime.run_prompt') as run:result=self.ask()
        self.assertTrue(result.recovery_required);run.assert_not_called()
        self.assertEqual(self.store.read_page(self.h).turns[0].status,'pending')

    def test_save_failure_withholds_answer_and_retry_does_not_rerun(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            with patch.object(self.store,'finish',side_effect=StorageError('capacity')):
                result=self.ask()
            retry=self.ask()
        self.assertEqual(result.state,'not_saved');self.assertTrue(result.recovery_required)
        self.assertIsNone(result.answer);self.assertIsNone(result.result)
        self.assertEqual(retry.state,'in_progress');self.assertEqual(run.call_count,1)

    def test_model_failure_saved_without_raw_error_or_automatic_retry(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeError('PRIVATE_ERROR_SECRET')) as run:
            result=self.ask();again=self.ask()
        self.assertEqual(result.stage,'model')
        self.assertNotIn('PRIVATE_ERROR_SECRET',result.model_dump_json())
        self.assertEqual(again.state,'failed');self.assertEqual(run.call_count,1)

    def test_failed_partial_and_oversized_turns_omitted_with_warning(self):
        self.seed('good')
        self.seed('partial',answer_status='partial')
        self.seed('long',answer='A'*2001)
        self.store.begin(self.h,'failed','旧失败问题');self.store.finish(self.h,'failed',error_code='timeout')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:result=self.ask()
        self.assertEqual(result.history.skipped_incomplete,2)
        self.assertEqual(result.history.skipped_oversized,1)
        prompt=data(run.call_args.args[0])['conversation']
        self.assertEqual(len(prompt['history']),2);self.assertTrue(prompt['history_incomplete'])
        self.assertIn('upstream_history_omitted',result.result.context_usage.issues)

    def test_loader_queries_once_and_respects_small_adapter_page_size(self):
        for i in range(4):self.seed(str(i))
        small=SQLiteChatStore(self.path,limits={'page_limit':1})
        with patch.object(small,'read_page',wraps=small.read_page) as read:
            with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):result=self.ask(store=small)
        self.assertEqual(read.call_count,1)
        self.assertEqual(result.history.rows_seen,1);self.assertTrue(result.history.older_available)

    def test_existing_history_budget_trims_whole_turns(self):
        for i in range(6):self.seed(str(i),answer='ANSWER-'+str(i))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            result=self.ask(conversation_limits={'max_turns':2,'max_history_chars':1000})
        self.assertEqual(result.result.context_usage.used_turns,2)
        h=data(run.call_args.args[0])['conversation']['history']
        self.assertEqual(h[1]['content'],'ANSWER-4');self.assertEqual(h[-1]['content'],'ANSWER-5')

    def test_forged_page_revision_rejected_before_model(self):
        self.seed()
        page=self.store.read_page(self.h)
        page.turns[0]=page.turns[0].model_copy(update={'scope_revision':999})
        with patch.object(self.store,'read_page',return_value=page):
            with patch('dsh_tutor.runtime.run_prompt') as run:result=self.ask()
        self.assertEqual(result.error_code,'history_unavailable');run.assert_not_called()

    def test_tool_scope_checked_before_persistence(self):
        from sandbox.tests.test_conversation import bridge
        value=bridge();value['context']['policy'].update(request_id='new',course_id='course-a')
        with patch('dsh_tutor.runtime.run_prompt') as run:result=self.ask(tool_bridge=value)
        self.assertEqual(result.stage,'input');run.assert_not_called()
        self.assertEqual(self.store.read_page(self.h).turns,[])

    def test_invalid_result_not_saved_as_final_answer(self):
        with patch('dsh_tutor.chat_session.answer_course',return_value={'answer':'not a full response'}):result=self.ask()
        self.assertEqual(result.error_code,'invalid_result')
        self.assertEqual(self.store.read_page(self.h).turns[0].status,'failed')

    def test_delete_during_model_prevents_resurrection(self):
        def transport(prompt,**kwargs):
            self.store.delete_history(self.h)
            return raw_answer(prompt,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):result=self.ask()
        self.assertEqual(result.state,'scope_changed');self.assertIsNone(result.answer)
        self.assertEqual(self.store.read_page(self.h).turns,[])

    def test_english_messages_and_partial_answer_persist_distinctly(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=lambda *a,**kw:dict(raw_answer(*a,**kw),status='partial')):
            result=self.ask(self.request(locale='en'))
        self.assertEqual(result.message,'The answer was saved.')
        self.assertEqual(result.answer_status,'partial')
        self.assertEqual(self.store.read_page(self.h).turns[0].answer_status,'partial')


if __name__=='__main__':unittest.main()
