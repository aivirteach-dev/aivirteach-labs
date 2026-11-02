"""第二阶段边界：实际线程/SQLite与可控阻塞，模型均替身；不证明真实吞吐。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch
import json,sqlite3,tempfile,time,unittest

from dsh_tutor.resources import ResourceManagedTutor
from dsh_tutor.chat_session import ChatSession,answer_chat
from dsh_tutor.budgets import PreparationBudget,storage_deadline
from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.scheduler_status import queue_projection
from sandbox.tests.test_storage import scope
from sandbox.tests.test_chat_session import raw_answer,data
from sandbox.tests.test_resources import wait_for


class PreparationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.store=SQLiteChatStore.create(self.root/'chat.db');self.h=self.store.activate(scope())
        self.service=self.service_with()
    def tearDown(self):self.tmp.cleanup()
    def service_with(self,**policy):
        return ResourceManagedTutor(policy={'min_free_bytes':0,**policy},config_path='unused',state_dir=self.root)
    def other(self,name):return self.store.activate(scope(student_id=name,learning_context_id=name))
    def q(self,rid='question'):
        return CourseQuestion(request_id=rid,question='继续',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
    def ask(self,handle=None,rid='question',**kw):
        return self.service.answer(self.q(rid),store=self.store,handle=handle or self.h,**kw)
    def seed(self,n=10):
        for i in range(n):
            self.store.begin(self.h,f'old-{i}',f'old-question-{i}');self.store.finish(self.h,f'old-{i}',answer=f'OLD-{i}',run_id=f'run-{i}')


class PreparationTests(PreparationFixture):

    def test_slow_history_does_not_hold_dsh_slot_or_block_ready_student(self):
        b=self.other('B');entered=Event();release=Event();read=self.store.read_page
        def slow(h,**kw):
            if h==self.h:entered.set();self.assertTrue(release.wait(3))
            return read(h,**kw)
        with patch.object(self.store,'read_page',side_effect=slow),patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(self.ask)
                try:
                    self.assertTrue(entered.wait(2));snap=self.service.gate.snapshot()
                    self.assertEqual((snap['preparing'],snap['active']),(1,0))
                    fast=pool.submit(self.ask,b).result(2)
                    self.assertEqual(fast.chat.state,'saved');self.assertFalse(a.done())
                finally:release.set()
                self.assertEqual(a.result(2).chat.state,'saved')

    def test_total_and_student_limits_apply_before_any_database_accept(self):
        self.service=self.service_with(max_waiting=2,max_preparing=1)
        handles=[self.h,self.other('B'),self.other('C'),self.other('D')];entered=Event();release=Event();read=self.store.read_page
        def slow(h,**kw):
            if h==self.h:entered.set();self.assertTrue(release.wait(3))
            return read(h,**kw)
        with patch.object(self.store,'read_page',side_effect=slow),patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):
            with ThreadPoolExecutor(max_workers=3) as pool:
                fs=[pool.submit(self.ask,handles[0])]
                try:
                    self.assertTrue(entered.wait(2))
                    for h in handles[1:3]:fs.append(pool.submit(self.ask,h))
                    wait_for(lambda:self.service.gate.snapshot()['preparation_waiting']==2)
                    self.assertEqual(self.service.gate.snapshot()['inflight'],3)
                    self.assertEqual(self.ask(handles[3]).admission,'queue_full')
                    self.assertEqual(self.ask().admission,'student_limit')
                    self.assertEqual(read(handles[3]).turns,[])
                finally:release.set()
                for f in fs:self.assertEqual(f.result(2).chat.state,'saved')
        self.assertEqual(self.service.gate.snapshot()['inflight'],0)

    def test_expired_blocking_adapter_keeps_prepare_slot_until_it_returns(self):
        self.service=self.service_with(max_preparing=1,preparation_timeout_seconds=.05)
        entered=Event();release=Event();read=self.store.read_page
        def slow(*a,**kw):entered.set();self.assertTrue(release.wait(3));return read(*a,**kw)
        with patch.object(self.store,'read_page',side_effect=slow),patch('dsh_tutor.runtime.run_prompt') as model:
            with ThreadPoolExecutor(max_workers=1) as pool:
                f=pool.submit(self.ask)
                try:
                    self.assertTrue(entered.wait(2));time.sleep(.08)
                    self.assertEqual((self.service.gate.snapshot()['preparing'],self.service.gate.snapshot()['inflight']),(1,1))
                    self.assertFalse(f.done())
                finally:release.set()
                r=f.result(2)
        self.assertEqual(r.chat.error_code,'preparation_timeout');model.assert_not_called()
        self.assertEqual(read(self.h).turns[-1].status,'failed');self.assertEqual(self.service.gate.snapshot()['inflight'],0)

    def test_waiting_for_preparation_timeout_does_not_create_pending(self):
        self.service=self.service_with(max_preparing=1,preparation_queue_timeout_seconds=.05)
        b=self.other('B');entered=Event();release=Event();read=self.store.read_page
        def slow(h,**kw):
            if h==self.h:entered.set();self.assertTrue(release.wait(3))
            return read(h,**kw)
        with patch.object(self.store,'read_page',side_effect=slow),patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(self.ask)
                try:
                    self.assertTrue(entered.wait(2));r=pool.submit(self.ask,b).result(1)
                    self.assertEqual(r.admission,'preparation_queue_timeout');self.assertEqual(read(b).turns,[])
                    self.assertEqual(self.service.gate.snapshot()['inflight'],1)
                finally:release.set()
                a.result(2)

    def test_execution_wait_cancellation_finishes_pending_and_retry_does_not_run(self):
        cancel=Event()
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                f=pool.submit(self.ask,cancel=cancel);wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                self.assertEqual(self.store.read_page(self.h).turns[-1].status,'pending');cancel.set()
                r=f.result(2);self.assertEqual(r.admission,'cancelled');self.assertFalse(r.chat.recovery_required)
                self.assertEqual(self.store.read_page(self.h).turns[-1].error_code,'cancelled')
            self.assertEqual(self.ask().chat.state,'failed');model.assert_not_called()

    def test_execution_wait_timeout_leaves_no_orphan_pending(self):
        self.service=self.service_with(queue_timeout_seconds=.05)
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                r=pool.submit(self.ask).result(1)
            self.assertEqual(r.admission,'queue_timeout');self.assertFalse(r.chat.recovery_required)
            self.assertEqual(self.store.read_page(self.h).turns[-1].status,'failed');model.assert_not_called()

    def test_cancel_cleanup_storage_failure_requires_recovery_and_no_automatic_model(self):
        cancel=Event()
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                f=pool.submit(self.ask,cancel=cancel);wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                with patch.object(self.store,'finish',side_effect=StorageError('busy')):
                    cancel.set();r=f.result(2)
            self.assertTrue(r.chat.recovery_required);self.assertEqual(self.ask().chat.state,'in_progress');model.assert_not_called()

    def test_scope_change_after_prepare_prevents_model_not_only_final_save(self):
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                f=pool.submit(self.ask);wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                self.store.activate(scope(instance_generation='g2'),expected_revision=1)
            r=f.result(2)
        self.assertEqual((r.chat.state,r.chat.stage),('scope_changed','revalidate'));model.assert_not_called()

    def test_history_delete_after_prepare_prevents_model_without_learning_memory(self):
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                f=pool.submit(self.ask);wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                self.store.delete_history(self.h)
            r=f.result(2)
        self.assertEqual(r.chat.error_code,'stale_request');model.assert_not_called()

    def test_default_reads_one_recent_page_not_sixteen_candidates(self):
        self.seed()
        with patch.object(self.store,'read_page',wraps=self.store.read_page) as read,patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:
            r=self.ask()
        self.assertEqual(read.call_count,1);self.assertEqual(read.call_args.kwargs['limit'],4)
        text=model.call_args.args[0];self.assertIn('OLD-9',text);self.assertNotIn('OLD-0',text)
        self.assertEqual((r.chat.history.mode,r.chat.history.rows_seen),('recent',4))

    def test_explicit_earlier_history_uses_scoped_anchor_and_single_bounded_query(self):
        self.seed(20)
        with patch.object(self.store,'read_page',wraps=self.store.read_page) as read,patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:
            r=self.ask(history_mode='earlier',history_before=5)
        self.assertEqual(read.call_count,1);self.assertEqual(read.call_args.kwargs['before'],5)
        self.assertLessEqual(read.call_args.kwargs['limit'],16);self.assertIn('OLD-0',model.call_args.args[0]);self.assertNotIn('OLD-19',model.call_args.args[0])
        self.assertEqual(r.chat.history.mode,'earlier');self.assertTrue(data(model.call_args.args[0])['conversation']['history_incomplete'])

    def test_earlier_without_anchor_really_omits_recent_window(self):
        self.seed()
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:r=self.ask(history_mode='earlier')
        self.assertEqual(r.chat.state,'saved');text=model.call_args.args[0]
        self.assertIn('OLD-5',text);self.assertNotIn('OLD-9',text)

    def test_earlier_lookup_cannot_read_another_student_even_with_their_sequence_anchor(self):
        other=self.other('other');self.store.begin(other,'foreign','PRIVATE_OTHER')
        row=self.store.finish(other,'foreign',answer='PRIVATE_OTHER',run_id='foreign')
        self.seed(3)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:
            r=self.ask(history_mode='earlier',history_before=row.sequence+1)
        self.assertEqual(r.chat.state,'saved');self.assertNotIn('PRIVATE_OTHER',model.call_args.args[0])

    def test_invalid_history_selector_fails_before_accept(self):
        with patch('dsh_tutor.runtime.run_prompt') as model:
            for kwargs in ({'history_mode':'all'},{'history_mode':'recent','history_before':2},{'history_mode':'earlier','history_before':True}):
                self.assertEqual(self.ask(**kwargs).chat.error_code,'invalid_input')
        self.assertEqual(self.store.read_page(self.h).turns,[]);model.assert_not_called()

    def test_prepared_snapshot_not_mutated_by_caller_and_same_session_runs_once(self):
        q=self.q();session=ChatSession(q,store=self.store,handle=self.h,config_path='unused',state_dir=self.root)
        self.assertIsNone(session.prepare());q.question='MUTATED';q.course.title='MUTATED'
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:r=session.execute()
        self.assertEqual(r.state,'saved');self.assertNotIn('MUTATED',model.call_args.args[0])
        with self.assertRaises(ValueError):session.execute()

    def test_abort_uses_original_request_id_after_caller_changes_object(self):
        q=self.q();session=ChatSession(q,store=self.store,handle=self.h,config_path='unused',state_dir=self.root)
        session.prepare();q.request_id='changed';session.abort('cancelled')
        self.assertEqual(self.store.read_page(self.h).turns[-1].error_code,'cancelled')

    def test_sqlite_query_interrupted_by_shared_deadline_and_context_reset(self):
        started=time.monotonic()
        with self.assertRaises(StorageError) as caught:
            with PreparationBudget(.03).storage_scope():
                with self.store._connection() as db:
                    db.execute('WITH RECURSIVE cnt(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM cnt WHERE x<100000000) SELECT SUM(x) FROM cnt').fetchone()
        self.assertEqual(caught.exception.code,'timeout');self.assertLess(time.monotonic()-started,.8)
        self.assertIsNone(storage_deadline.get());self.assertEqual(self.store.read_page(self.h).turns,[])

    def test_sqlite_write_lock_wait_respects_prepare_budget(self):
        self.service=self.service_with(preparation_timeout_seconds=.05)
        lock=sqlite3.connect(self.store.path);lock.execute('BEGIN IMMEDIATE');started=time.monotonic()
        try:
            with patch('dsh_tutor.runtime.run_prompt') as model:r=self.ask()
        finally:lock.rollback();lock.close()
        self.assertEqual(r.chat.error_code,'preparation_timeout');self.assertLess(time.monotonic()-started,.8);model.assert_not_called()

    def test_model_health_not_inferred_from_prepare_or_execution_metrics(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):self.ask()
        result=queue_projection(self.service.gate.snapshot())
        self.assertEqual(result['inflight'],0);self.assertEqual(result['preparation_seconds']['samples'],1)
        self.assertNotIn('model_health',result);self.assertNotIn('student-a',json.dumps(result))

    def test_lower_preparation_concurrency_drains_existing_reads(self):
        self.service=self.service_with(max_preparing=2);b=self.other('B');c=self.other('C')
        entered=[Event(),Event(),Event()];release=[Event(),Event()];read=self.store.read_page
        def slow(h,**kw):
            i=[self.h,b,c].index(h);entered[i].set()
            if i<2:self.assertTrue(release[i].wait(3))
            return read(h,**kw)
        with patch.object(self.store,'read_page',side_effect=slow),patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer),ThreadPoolExecutor(max_workers=3) as pool:
            a=pool.submit(self.ask);bf=pool.submit(self.ask,b)
            try:
                self.assertTrue(entered[0].wait(2));self.assertTrue(entered[1].wait(2))
                cf=pool.submit(self.ask,c);wait_for(lambda:self.service.gate.snapshot()['preparation_waiting']==1)
                self.service.gate.reconfigure({'max_preparing':1},expected_revision=1)
                release[0].set();a.result(2);self.assertFalse(entered[2].is_set())
            finally:
                for event in release:event.set()
            self.assertEqual(bf.result(2).chat.state,'saved');self.assertEqual(cf.result(2).chat.state,'saved')


class MemoryPreparationTests(PreparationFixture):
    """任务记忆用例复用夹具，不依赖学生自然语言或模型推测来判定版本。"""
    def setUp(self):
        super().setUp();self.store.migrate_learning_memory();migrate_task_memory(self.store)
        self.repo=TaskRepository(self.store);self.repo.create(self.h,'A','任务A');self.repo.create(self.h,'B','任务B')
        self.a=self.repo.bind(self.h,'A');self.b=self.repo.bind(self.h,'B')
        self.a.edit_learning(self.h,'goal','OLD_GOAL',expected_revision=0,request_id='goal')

    def task_ask(self,rid='question',**kw):
        return self.service.answer(self.q(rid),store=self.a,handle=self.h,**kw)

    def test_memory_edit_during_execution_wait_blocks_old_prompt_before_model(self):
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                f=pool.submit(self.task_ask);wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                self.a.edit_learning(self.h,'goal','NEW_GOAL',expected_revision=1,request_id='edit')
            r=f.result(2)
        self.assertEqual((r.chat.state,r.chat.stage,r.chat.error_code),('scope_changed','revalidate','memory_changed'));model.assert_not_called()
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:self.task_ask('new')
        self.assertIn('NEW_GOAL',model.call_args.args[0]);self.assertNotIn('OLD_GOAL',model.call_args.args[0])

    def test_memory_delete_during_execution_wait_cannot_resurrect_goal(self):
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.gate.slot('blocker'):
                f=pool.submit(self.task_ask);wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                self.a.forget_learning(self.h,'goal',expected_revision=1)
            r=f.result(2)
        self.assertEqual(r.chat.error_code,'memory_changed');model.assert_not_called()

    def test_edit_during_history_read_is_detected_before_entering_dsh_queue(self):
        read=self.a.read_page
        def edit(*a,**kw):
            page=read(*a,**kw);self.a.edit_learning(self.h,'goal','NEW_GOAL',expected_revision=1,request_id='edit');return page
        with patch.object(type(self.a),'read_page',side_effect=edit),patch('dsh_tutor.runtime.run_prompt') as model:r=self.task_ask()
        self.assertEqual((r.chat.state,r.chat.stage),('scope_changed','prepare'));model.assert_not_called()
        self.assertEqual(self.service.gate.snapshot()['counts'].get('admitted',0),0)

    def test_targeted_older_lookup_still_respects_memory_deletion_floor(self):
        self.a.begin(self.h,'old','OLD_SECRET');self.a.finish(self.h,'old',answer='OLD_SECRET',run_id='old')
        self.a.edit_learning(self.h,'goal','NEW_GOAL',expected_revision=1,request_id='edit')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as model:
            r=self.task_ask(history_mode='earlier',history_before=3)
        self.assertEqual(r.chat.state,'saved');self.assertNotIn('OLD_SECRET',model.call_args.args[0]);self.assertIn('NEW_GOAL',model.call_args.args[0])


if __name__=='__main__':unittest.main()
