"""任务隔离、显式兼容、管理CAS和真正问答路径；模型传输替身，无Mac请求。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.learning_memory import MemoryLimits
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.resources import ResourceManagedTutor
from sandbox.tests.test_storage import scope
from sandbox.tests.test_chat_session import raw_answer,data


class TaskMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'tasks.db'
        self.store=SQLiteChatStore.create(self.path);self.h=self.store.activate(scope())
        self.store.migrate_learning_memory();migrate_task_memory(self.store)
        self.repo=TaskRepository(self.store)
        for task in ('A','B'):self.repo.create(self.h,task,'任务'+task)
        self.a=self.repo.bind(self.h,'A');self.b=self.repo.bind(self.h,'B')

    def tearDown(self):self.temp.cleanup()

    def remember(self,bound,q,rid='seed'):
        bound.begin(bound.handle,rid,q)
        items=bound.remember_question(bound.handle,rid,q,limits=MemoryLimits())
        bound.finish(bound.handle,rid,answer='原始确认')
        return items

    def ask(self,bound=None,rid='follow',q='继续当前练习',**kwargs):
        bound=bound or self.a
        req=CourseQuestion(request_id=rid,question=q,course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
        return answer_chat(req,store=bound,handle=bound.handle,learning_memory=bound,
                           config_path='unused',state_dir=self.temp.name,**kwargs)

    def edit(self,bound,kind,text,rid='edit',rev=0):
        return bound.edit_learning(bound.handle,kind,text,expected_revision=rev,request_id=rid)

    def test_explicit_migration_no_silent_old_assignment(self):
        p=Path(self.temp.name)/'old.db';s=SQLiteChatStore.create(p);h=s.activate(scope())
        with self.assertRaises(StorageError):migrate_task_memory(s)
        s.migrate_learning_memory();s.begin(h,'old','学习目标：OLD_UNASSIGNED')
        s.remember_question(h,'old','学习目标：OLD_UNASSIGNED',limits=MemoryLimits());s.finish(h,'old',answer='原回答')
        migrate_task_memory(s);migrate_task_memory(s)
        repo=TaskRepository(s);t=repo.ensure_lesson_task(h);bound=repo.bind(h,t['task_id'])
        self.assertEqual(bound.learning_items(h),[]);self.assertEqual(bound.read_page(h).turns,[])
        self.assertEqual(repo.unassigned(h)['memory'][0]['quote'],'OLD_UNASSIGNED')
        self.assertEqual(s.read_page(h).turns[0].answer,'原回答')
        with self.assertRaises(StorageError) as e:s.learning_items(h)
        self.assertEqual(e.exception.code,'task_binding_required')

    def test_task_default_stable_and_does_not_follow_vm_identity(self):
        first=self.repo.ensure_lesson_task(self.h)
        new=self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        self.assertEqual(first,self.repo.ensure_lesson_task(new))

    def test_task_creation_idempotency_conflict_and_limit(self):
        first=self.repo.create(self.h,'A','任务A');self.assertEqual(first,self.repo.create(self.h,'A','任务A'))
        with self.assertRaises(StorageError):self.repo.create(self.h,'A','覆盖标题')
        with self.assertRaises(StorageError):TaskRepository(self.store,{'max_tasks':2}).create(self.h,'C','任务C')

    def test_task_pagination_is_bounded(self):
        repo=TaskRepository(self.store,{'page_limit':1});one=repo.list(self.h)
        two=repo.list(self.h,after=one['next_after'])
        self.assertEqual([one['tasks'][0]['task_id'],two['tasks'][0]['task_id']],['A','B'])
        self.assertIsNone(two['next_after'])
        with self.assertRaises(StorageError):repo.list(self.h,limit=2)

    def test_foreign_student_context_course_and_lesson_denied(self):
        foreign=self.h.model_copy(update={'scope':scope(student_id='other')})
        with self.assertRaises(StorageError):self.repo.bind(foreign,'A')
        new=self.store.activate(scope(lesson_id='l2'),expected_revision=1)
        self.assertEqual(self.repo.list(new)['tasks'],[])
        with self.assertRaises(StorageError):self.repo.bind(new,'A')
        with self.assertRaises(StorageError):self.a.inspect(self.h)

    def test_task_and_handle_public_binding_cannot_be_reassigned(self):
        with self.assertRaises(AttributeError):self.a.task_id='B'
        with self.assertRaises(AttributeError):self.a.handle=self.h

    def test_task_memory_and_history_never_mix_default(self):
        self.remember(self.a,'学习目标：A_CANARY\n偏好：简短中文')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:r=self.ask(self.b)
        self.assertEqual(r.state,'saved');prompt=run.call_args.args[0]
        self.assertNotIn('A_CANARY',prompt);self.assertIn('简短中文',prompt)
        self.assertEqual(data(prompt)['conversation']['history'],[])

    def test_returning_to_old_task_recovers_its_own_goal(self):
        self.remember(self.a,'学习目标：A_CANARY','a')
        self.remember(self.b,'学习目标：B_CANARY','b')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:r=self.ask(self.a)
        self.assertEqual(r.state,'saved');self.assertIn('A_CANARY',run.call_args.args[0]);self.assertNotIn('B_CANARY',run.call_args.args[0])

    def test_duplicate_request_cannot_be_rebound_to_other_task(self):
        self.remember(self.a,'普通问题','same')
        with self.assertRaises(StorageError) as e:self.b.accept(self.h,'same','普通问题')
        self.assertEqual(e.exception.code,'request_conflict')
        with patch('dsh_tutor.runtime.run_prompt') as run:r=self.ask(self.a,'same','普通问题')
        self.assertEqual(r.state,'replayed');run.assert_not_called()

    def test_global_selected_task_does_not_exist_and_opening_other_tab_keeps_running_task(self):
        def transport(*args,**kwargs):
            self.repo.bind(self.h,'B')
            return raw_answer(*args,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):r=self.ask(self.a)
        self.assertEqual(r.state,'saved');self.assertEqual(len(self.b.read_page(self.h).turns),0)
        self.assertEqual(len(self.a.read_page(self.h).turns),1)

    def test_vm_change_keeps_goal_and_preference_but_stales_environment_self_reports(self):
        self.remember(self.a,'学习目标：保留目标\n已尝试：刷新\n未解决：空白\n偏好：中文')
        new=self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        a=self.repo.bind(new,'A')
        self.assertEqual({x.kind for x in a.learning_items(new)},{'goal','preference'})
        self.assertEqual({r['state'] for r in a.inspect(new) if r['kind'] in ('attempt','open_issue')},{'stale_environment'})
        self.assertEqual(a.read_page(new).turns,[])

    def test_goal_change_marks_related_attempts_for_review(self):
        self.remember(self.a,'学习目标：旧目标\n已尝试：刷新\n未解决：空白')
        self.edit(self.a,'goal','新目标',rev=1)
        self.assertEqual([x.kind for x in self.a.learning_items(self.h)],['goal'])
        for x in self.a.inspect(self.h):
            if x['kind']!='goal':self.assertEqual(x['state'],'needs_review');self.assertEqual(x['revision'],2)
        self.edit(self.a,'attempt','刷新',rid='confirm',rev=2)
        self.assertEqual({x.kind for x in self.a.learning_items(self.h)},{'goal','attempt'})

    def test_same_goal_does_not_invalidate_attempts(self):
        self.remember(self.a,'学习目标：同一个\n已尝试：刷新','one')
        self.remember(self.a,'学习目标：同一个','two')
        self.assertEqual({x.kind for x in self.a.learning_items(self.h)},{'goal','attempt'})

    def test_same_message_goal_processed_before_new_attempt(self):
        self.remember(self.a,'学习目标：旧目标\n已尝试：旧尝试','old')
        self.remember(self.a,'已尝试：新尝试\n学习目标：新目标','new')
        self.assertEqual({x.kind:x.text for x in self.a.learning_items(self.h)},{'goal':'新目标','attempt':'新尝试'})

    def test_management_edit_source_offsets_timestamp_and_no_model(self):
        with patch('dsh_tutor.runtime.run_prompt') as run:rows=self.edit(self.a,'goal','目标<script>')
        run.assert_not_called();x=rows[0];self.assertEqual(x['source'],'student_statement')
        row=self.a.read_page(self.h).turns[0]
        self.assertEqual(row.question[x['source_start']:x['source_end']],x['text'])
        self.assertEqual(row.error_code,'memory_edit');self.assertIsNone(row.answer);self.assertTrue(x['updated_at'])

    def test_stale_management_revision_does_not_overwrite_new(self):
        self.edit(self.a,'goal','初始')
        self.edit(self.a,'goal','新值',rid='second',rev=1)
        with self.assertRaises(StorageError) as e:self.edit(self.a,'goal','旧页面覆盖',rid='stale',rev=1)
        self.assertEqual(e.exception.code,'revision_conflict');self.assertEqual(self.a.learning_items(self.h)[0].text,'新值')

    def test_two_concurrent_edits_one_cas_wins(self):
        self.edit(self.a,'goal','初始')
        def change(i):
            try:self.edit(self.a,'goal','值'+str(i),rid=str(i),rev=1);return 'ok'
            except StorageError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(change,range(2)))
        self.assertCountEqual(results,['ok','revision_conflict'])

    def test_revoke_removes_prompt_echo_and_cannot_delete_newer_value(self):
        self.remember(self.a,'学习目标：REVOKE_CANARY')
        self.a.begin(self.h,'echo','目标是什么');self.a.finish(self.h,'echo',answer='REVOKE_CANARY')
        self.a.forget_learning(self.h,'goal',expected_revision=1)
        self.assertEqual(self.a.inspect(self.h)[0]['text'],'')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:r=self.ask()
        self.assertNotIn('REVOKE_CANARY',run.call_args.args[0]);self.assertEqual(r.state,'saved')
        with self.assertRaises(StorageError):self.a.forget_learning(self.h,'goal',expected_revision=1)

    def test_edit_while_model_runs_withholds_stale_answer(self):
        self.edit(self.a,'goal','旧目标')
        def transport(*args,**kwargs):
            self.edit(self.a,'goal','新目标',rid='edit-during',rev=1)
            return raw_answer(*args,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):r=self.ask()
        self.assertEqual(r.error_code,'memory_changed');self.assertIsNone(r.answer)

    def test_unrelated_task_edit_does_not_invalidate_running_answer(self):
        def transport(*args,**kwargs):
            self.edit(self.b,'goal','另一个任务')
            return raw_answer(*args,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):r=self.ask()
        self.assertEqual(r.state,'saved')

    def test_shared_preference_edit_invalidates_running_answer_in_other_task(self):
        def transport(*args,**kwargs):
            self.edit(self.b,'preference','英文')
            return raw_answer(*args,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):r=self.ask()
        self.assertEqual(r.error_code,'memory_changed')

    def test_atomic_finish_checks_task_epoch(self):
        self.a.begin(self.h,'pending','问答');epoch=self.a.learning_state(self.h)[1]
        self.edit(self.a,'goal','新目标')
        with self.assertRaises(StorageError) as e:self.a.finish(self.h,'pending',answer='旧上下文',expected_memory_epoch=epoch)
        self.assertEqual(e.exception.code,'memory_changed')
        self.assertEqual(next(t.status for t in self.a.read_page(self.h).turns if t.request_id=='pending'),'pending')

    def test_task_bound_source_cannot_reference_other_task(self):
        self.a.begin(self.h,'source','学习目标：A')
        with self.assertRaises(StorageError):self.b.remember_question(self.h,'source','学习目标：A',limits=MemoryLimits())

    def test_source_history_delete_invalidates_current_task_and_shared_preference(self):
        self.remember(self.a,'学习目标：原目标\n偏好：中文')
        new=self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        a=self.repo.bind(new,'A');self.assertEqual(len(a.learning_items(new)),2)
        self.store.delete_history(self.h)
        self.assertEqual(a.learning_items(new),[]);self.assertEqual(self.repo.bind(new,'B').learning_items(new),[])

    def test_budget_and_resource_wrapper_reused_no_extra_model_calls(self):
        self.edit(self.a,'goal','目标')
        service=ResourceManagedTutor(policy={},config_path='unused',state_dir=self.temp.name)
        q=CourseQuestion(request_id='managed',question='继续',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            result=service.answer(q,store=self.a,handle=self.h,learning_memory=self.a)
        self.assertEqual(result.chat.state,'saved');self.assertEqual(run.call_count,1)
        self.assertLessEqual(len(json.dumps(data(run.call_args.args[0])['conversation']['learning_memory'],ensure_ascii=False)),1200)

    def test_no_implicit_task_inference_or_natural_language_memory_overwrite(self):
        self.edit(self.a,'goal','保持目标')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):self.ask(q='这个先不弄了，我也许想换个练习')
        self.assertEqual(len(self.repo.list(self.h)['tasks']),2)
        self.assertEqual(self.a.learning_items(self.h)[0].text,'保持目标')

    def test_restart_restores_binding_and_memory_without_in_memory_session(self):
        self.edit(self.a,'goal','PROCESS_CANARY')
        code='''
import sys
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.task_memory import TaskRepository
s=SQLiteChatStore(sys.argv[1]);h=s.current(student_id='student-a',learning_context_id='enrollment-a')
a=TaskRepository(s).bind(h,'A')
assert a.learning_items(h)[0].text=='PROCESS_CANARY'
assert TaskRepository(s).bind(h,'B').learning_items(h)==[]
'''
        proc=subprocess.run([sys.executable,'-c',code,str(self.path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)

    def test_management_validation_and_rollback(self):
        for value in ('a\n偏好：越权','a'*301,''):
            with self.assertRaises(StorageError):self.edit(self.a,'goal',value)
        self.assertEqual(self.a.inspect(self.h),[]);self.assertEqual(self.a.read_page(self.h).turns,[])

    def test_task_adapter_auto_enables_memory_and_reply_carries_task(self):
        self.edit(self.a,'goal','必须携带')
        req=CourseQuestion(request_id='automatic',question='继续',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            r=answer_chat(req,store=self.a,handle=self.h,config_path='unused',state_dir=self.temp.name)
        self.assertEqual(r.task_id,'A');self.assertEqual(r.state,'saved')
        self.assertIn('必须携带',run.call_args.args[0])

    def test_v3_unbound_application_rejected_before_model_and_history(self):
        req=CourseQuestion(request_id='unbound',question='继续',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
        with patch('dsh_tutor.runtime.run_prompt') as run:
            r=answer_chat(req,store=self.store,handle=self.h,config_path='unused',state_dir=self.temp.name)
        self.assertEqual(r.error_code,'task_binding_required');run.assert_not_called()
        self.assertEqual(self.store.read_page(self.h).turns,[])

    def test_migration_refuses_pending_and_rolls_back(self):
        p=Path(self.temp.name)/'pending.db';s=SQLiteChatStore.create(p);h=s.activate(scope());s.migrate_learning_memory()
        s.begin(h,'pending','旧请求')
        with self.assertRaises(StorageError) as e:migrate_task_memory(s)
        self.assertEqual(e.exception.code,'requests_pending')
        with sqlite3.connect(p) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='learning_tasks'").fetchone())
        s.finish(h,'pending',error_code='interrupted');migrate_task_memory(s)


if __name__=='__main__':unittest.main()
