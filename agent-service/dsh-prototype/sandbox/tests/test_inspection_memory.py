"""真实SQLite与工具授权入口，模拟观察/推理；验证证据、原子性和历史隔离。"""
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dsh_tutor.storage import SQLiteChatStore,StorageError,StorageLimits
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.lifecycle import migrate_lifecycle,TaskLifecycle
from dsh_tutor.inspection_memory import InspectionRepository,migrate_inspections
from dsh_tutor.maintenance import LocalMaintenance
from dsh_tutor.task_memory_api import TaskMemoryAPI
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.chat_session import answer_chat
from sandbox.mcp_acceptance import bridge_for
from sandbox.tests.test_storage import scope
from sandbox.tests.test_chat_session import raw_answer,data


class InspectionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=SQLiteChatStore.create(self.root/'chat.db');self.h=self.store.activate(scope())
        self.store.migrate_learning_memory();migrate_task_memory(self.store);migrate_lifecycle(self.store);migrate_inspections(self.store)
        self.repo=TaskRepository(self.store);self.repo.create(self.h,'A','任务A');self.repo.create(self.h,'B','任务B')
        self.a=self.repo.bind(self.h,'A');self.b=self.repo.bind(self.h,'B');self.history=InspectionRepository(self.store)

    def tearDown(self):self.temp.cleanup()

    def request(self,rid='check'):
        s=self.h.scope
        return CourseQuestion(request_id=rid,question='检查当前服务',course=dict(course_id=s.course_id,version=s.course_version,lesson_id=s.lesson_id,title='虚构'))

    def bridge(self,rid='check',service='n8n'):
        s=self.h.scope
        bridge=bridge_for(s.course_id,service,'running',request_id=rid,instance_id=s.instance_id)
        bridge['context']['policy'].update(course_version=s.course_version,student_id=s.student_id,learning_context_id=s.learning_context_id,lesson_id=s.lesson_id,instance_generation=s.instance_generation)
        return bridge

    def transport(self,bridge,**kwargs):
        from sandbox.inspection_fixtures import simulated_transport
        return simulated_transport(bridge,self.root,**kwargs)

    def ask(self,rid='check',*,bound=None,bridge=None,transport=None):
        bound=bound or self.a
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport or raw_answer) as run:
            reply=answer_chat(self.request(rid),store=bound,handle=self.h,tool_bridge=bridge,config_path='unused',state_dir=self.root)
            return reply,run

    def checked(self,rid='check',**kwargs):
        bridge=self.bridge(rid)
        return self.ask(rid,bridge=bridge,transport=self.transport(bridge,**kwargs))[0]

    def test_success_atomically_saved_reopened_and_note_not_copied(self):
        r=self.checked();self.assertEqual(r.state,'saved')
        reopened=InspectionRepository(SQLiteChatStore(self.store.path));record=reopened.get(self.h,'A','check')
        self.assertEqual(record['record_status'],'verified');self.assertEqual(record['snapshot']['observations'][0]['state'],'running')
        self.assertFalse(record['usable_as_current']);self.assertTrue(record['always_historical'])
        self.assertNotIn('PRIVATE_BACKEND_NOTE',json.dumps(record));self.assertEqual(record['snapshot']['tool_contract_version'],'service-status-v1')
        self.assertIsNone(record['snapshot']['backend_version'])

    def test_failed_tool_has_failure_trace_without_fake_service_state(self):
        r=self.checked(failure=True);self.assertEqual(r.state,'saved');self.assertEqual(r.answer_status,'partial')
        snapshot=self.history.get(self.h,'A','check')['snapshot']
        self.assertEqual(snapshot['status'],'no_observation');self.assertEqual(snapshot['observations'],[])
        self.assertEqual(snapshot['attempts'][0]['status'],'failed');self.assertNotIn('PRIVATE_BACKEND_ERROR',json.dumps(snapshot))

    def test_denied_tool_name_is_normalized_and_not_executed(self):
        self.checked(denied=True)
        snapshot=self.history.get(self.h,'A','check')['snapshot']
        self.assertEqual(snapshot['reported_executions'],0);self.assertEqual(snapshot['attempts'][0]['tool'],'unsupported_tool')
        self.assertNotIn('PRIVATE_UNAUTHORIZED_TOOL',json.dumps(snapshot))

    def test_wrong_evidence_rejected_without_foreign_data_persisted(self):
        self.checked(mutate=lambda r:r['evidence'][0].update(instance_id='FOREIGN_INSTANCE'))
        record=self.history.get(self.h,'A','check')
        self.assertEqual(record['record_status'],'rejected');self.assertEqual(record['snapshot']['observations'],[])
        self.assertNotIn('FOREIGN_INSTANCE',json.dumps(record))

    def test_plain_answer_classified_no_tools_and_restored_as_history(self):
        one,_=self.ask('plain');two,run=self.ask('follow')
        self.assertEqual(one.state,'saved');self.assertEqual(two.state,'saved')
        self.assertEqual(self.history.get(self.h,'A','plain')['record_status'],'no_tools')
        self.assertEqual(len(data(run.call_args.args[0])['conversation']['history']),2)

    def test_tool_fact_turn_not_in_automatic_followup_prompt(self):
        self.checked()
        reply,run=self.ask('follow')
        self.assertEqual(reply.state,'saved');self.assertGreater(reply.history.skipped_inspection,0)
        self.assertEqual(data(run.call_args.args[0])['conversation']['history'],[])
        self.assertNotIn('正在运行',run.call_args.args[0])

    def test_duplicate_replays_answer_without_model_or_new_record(self):
        first=self.checked()
        bridge=self.bridge()
        with patch('dsh_tutor.runtime.run_prompt') as run:
            second=answer_chat(self.request(),store=self.a,handle=self.h,tool_bridge=bridge,config_path='unused',state_dir=self.root)
        run.assert_not_called();self.assertEqual(second.state,'replayed');self.assertEqual(first.answer,second.answer)
        self.assertEqual(len(self.history.list(self.h,'A')['records']),1)

    def test_task_and_student_scope_deny_history_access(self):
        self.checked()
        with self.assertRaises(StorageError):self.history.get(self.h,'B','check')
        other=self.h.model_copy(update={'scope':scope(student_id='other')})
        with self.assertRaises(StorageError):self.history.get(other,'A','check')

    def test_vm_change_keeps_history_but_marks_environment_mismatch(self):
        self.checked()
        new=self.store.activate(scope(instance_generation='g2',environment_os='windows'),expected_revision=1)
        record=self.history.get(new,'A','check')
        self.assertFalse(record['current_environment_matches']);self.assertEqual(record['observed_environment']['environment_os'],'linux')
        self.assertFalse(record['usable_as_current'])

    def test_archived_history_viewable_restore_never_makes_it_current(self):
        self.checked();life=TaskLifecycle(self.store)
        for operation,target in [('a','archived'),('b','active')]:
            p=life.preview(self.h,'A',target);life.apply(self.h,'A',target,preview_token=p['preview_token'],operation_id=operation)
            self.assertFalse(self.history.get(self.h,'A','check')['usable_as_current'])

    def test_failure_then_old_success_not_used_as_current(self):
        self.checked('first',state='running');r=self.checked('second',failure=True)
        record=self.history.get(self.h,'A','second')
        self.assertEqual(record['snapshot']['observations'],[]);self.assertNotIn('正在运行',r.answer)
        self.assertTrue(self.history.get(self.h,'A','first')['snapshot']['observations'])

    def test_two_os_two_courses_use_same_history_protocol(self):
        for i,os_name in enumerate(['linux','windows']):
            if i:
                self.h=self.store.activate(scope(environment_os=os_name,course_id='course-b',lesson_id='l2'),expected_revision=1)
                self.repo.create(self.h,'C','其他课程');self.a=self.repo.bind(self.h,'C')
            r=self.checked(str(i))
            record=self.history.get(self.h,'A' if i==0 else 'C',str(i))
            self.assertEqual(r.state,'saved');self.assertEqual(record['observed_environment']['environment_os'],os_name)
            self.assertEqual(record['snapshot']['observations'][0]['source'],'simulation')

    def test_capacity_failure_rolls_back_answer_and_evidence(self):
        self.store.limits=StorageLimits(max_inspection_bytes=1024)
        bridge=self.bridge()
        original=self.transport(bridge)
        def many(prompt,**kwargs):
            # 放大合法结果的模型标签以外不能突破摘要约束，使用真实的多次工具轨迹。
            result=original(prompt,**kwargs)
            for i in range(2,4):
                item=dict(result['evidence'][0],evidence_id='e'+str(i))
                result['evidence'].append(item)
                result['tool_trace'].append(dict(result['tool_trace'][0],attempt=i,evidence=item))
            result.update(tool_calls=3,tool_attempts=3)
            result['answer']=json.dumps({'evidence_ids':[x['evidence_id'] for x in result['evidence']],'course_instruction_indexes':[],'next_step':'none'})
            return result
        r,_=self.ask(bridge=bridge,transport=many)
        self.assertEqual(r.state,'not_saved');self.assertEqual(r.error_code,'inspection_capacity');self.assertIsNone(r.answer)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inspection_records').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM turns').fetchone()[0],'pending')

    def test_atomic_sql_error_after_record_insert_rolls_back_both(self):
        with self.store._connection(write=True) as db:
            db.execute("CREATE TRIGGER block_final BEFORE UPDATE OF answer ON turns WHEN NEW.answer IS NOT NULL BEGIN SELECT RAISE(ABORT,'blocked'); END")
        r=self.checked();self.assertEqual(r.state,'not_saved')
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inspection_records').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM turns').fetchone()[0],'pending')

    def test_forged_no_tool_evidence_rejected_before_save(self):
        def forged(*args,**kwargs):return raw_answer(*args,**kwargs)|{'tool_calls':1,'tool_attempts':1}
        r,_=self.ask(transport=forged)
        self.assertEqual(r.state,'failed');self.assertEqual(r.stage,'result')
        self.assertEqual(self.history.get(self.h,'A','check')['record_status'],'no_final_result')

    def test_delete_source_history_removes_inspection_payload(self):
        self.checked();self.store.delete_history(self.h)
        record=self.history.get(self.h,'A','check')
        self.assertEqual(record['record_status'],'history_deleted');self.assertNotIn('snapshot',record)
        self.assertEqual(self.history.list(self.h,'A')['records'],[])
        with self.store._connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM inspection_records').fetchone()[0],0)

    def test_corrupt_record_rejected_and_not_rendered(self):
        self.checked()
        with self.store._connection(write=True) as db:db.execute("UPDATE inspection_records SET body_json='{}'")
        with self.assertRaises(StorageError) as e:self.history.get(self.h,'A','check')
        self.assertEqual(e.exception.code,'inspection_corrupt')

    def test_plain_history_corruption_blocks_inference(self):
        self.ask('plain')
        with self.store._connection(write=True) as db:db.execute("UPDATE inspection_records SET body_sha256='wrong'")
        r,run=self.ask('follow');self.assertEqual(r.stage,'history');run.assert_not_called()

    def test_pending_migration_refused_and_legacy_records_not_backfilled(self):
        path=self.root/'old.db';s=SQLiteChatStore.create(path);h=s.activate(scope());s.migrate_learning_memory();migrate_task_memory(s);migrate_lifecycle(s)
        repo=TaskRepository(s);repo.create(h,'A','旧任务');a=repo.bind(h,'A');a.begin(h,'old','旧问题')
        with self.assertRaises(StorageError):migrate_inspections(s)
        a.finish(h,'old',answer='OLD_UNCLASSIFIED_CANARY');migrate_inspections(s);migrate_inspections(s)
        history=InspectionRepository(s);self.assertEqual(history.get(h,'A','old')['record_status'],'not_recorded')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            r=answer_chat(self.request('follow'),store=a,handle=h,config_path='unused',state_dir=self.root)
        self.assertEqual(r.state,'saved');self.assertNotIn('OLD_UNCLASSIFIED_CANARY',run.call_args.args[0])

    def test_new_v5_direct_finish_requires_snapshot(self):
        self.a.begin(self.h,'pending','问题')
        with self.assertRaises(StorageError) as e:self.a.finish(self.h,'pending',answer='缺少记录')
        self.assertEqual(e.exception.code,'inspection_required')

    def test_paging_no_cross_task_and_max_limit(self):
        for i in range(3):self.ask(str(i))
        first=self.history.list(self.h,'A',limit=2);second=self.history.list(self.h,'A',before=first['next_before'],limit=2)
        self.assertEqual(len(first['records']),2);self.assertEqual(len(second['records']),1)
        self.assertEqual(self.history.list(self.h,'B')['records'],[])
        with self.assertRaises(StorageError):self.history.list(self.h,'A',limit=21)

    def test_fresh_process_recovers_structured_record(self):
        self.checked()
        code="""
import sys
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.inspection_memory import InspectionRepository
s=SQLiteChatStore(sys.argv[1]);h=s.current(student_id='student-a',learning_context_id='enrollment-a')
r=InspectionRepository(s).get(h,'A','check')
assert r['snapshot']['observations'][0]['state']=='running'
assert r['usable_as_current'] is False
"""
        p=subprocess.run([sys.executable,'-c',code,str(self.store.path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)

    def test_backup_restores_evidence_but_copy_stays_quarantined(self):
        self.checked();engine=LocalMaintenance(self.store,self.root/'maintenance',{'min_free_bytes':0})
        backup=engine.create_backup();report=engine.validate_restore(backup['backup_id'])
        self.assertEqual(report['restored_stats']['inspection_records'],1)
        path=engine.root/'validations'/report['validation_id']/'snapshot.sqlite'
        with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT kind FROM inspection_records').fetchone()[0],'verified')
        with self.assertRaises(StorageError):SQLiteChatStore(path)

    def test_student_api_history_access_has_no_tool_execution(self):
        self.checked();api=TaskMemoryAPI(self.repo)
        with patch('dsh_tutor.runtime.run_prompt') as run:
            listing=api.execute(self.h,dict(action='inspections',task_id='A'))
            detail=api.execute(self.h,dict(action='inspection_detail',task_id='A',request_id='check'))
        run.assert_not_called();self.assertEqual(len(listing['records']),1);self.assertTrue(detail['always_historical'])

    def test_current_request_still_runs_fresh_check(self):
        self.checked('old',state='running');r=self.checked('new',state='stopped')
        self.assertIn('已停止',r.answer);self.assertNotIn('正在运行',r.answer)
        self.assertEqual(self.history.get(self.h,'A','old')['snapshot']['observations'][0]['state'],'running')
        self.assertEqual(self.history.get(self.h,'A','new')['snapshot']['observations'][0]['state'],'stopped')

    def test_oversized_history_cursor_rejected_as_input_error(self):
        with self.assertRaises(StorageError) as e:self.history.list(self.h,'A',before=2**70)
        self.assertEqual(e.exception.code,'invalid_input')
        with self.assertRaises(StorageError):TaskMemoryAPI(self.repo).execute(self.h,dict(action='inspections',task_id='A',before=2**70))

    def test_non_default_course_version_bound_without_guessing(self):
        self.h=self.store.activate(scope(course_version='import-example-v5'),expected_revision=1)
        self.repo.create(self.h,'custom-version','不同版本');self.a=self.repo.bind(self.h,'custom-version')
        r=self.checked()
        self.assertEqual(r.state,'saved');self.assertEqual(self.history.get(self.h,'custom-version','check')['record_status'],'verified')


if __name__=='__main__':unittest.main()
