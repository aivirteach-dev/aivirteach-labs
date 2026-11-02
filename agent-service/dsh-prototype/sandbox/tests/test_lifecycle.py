"""生命周期/诊断/备份实测：真实SQLite及并发，模型传输替身，零Mac请求。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.lifecycle import TaskLifecycle,migrate_lifecycle
from dsh_tutor.diagnostics import DiagnosticStore,RequestTrace
from dsh_tutor.maintenance import LocalMaintenance,VALIDATION_ID
from dsh_tutor.task_memory_api import TaskMemoryAPI
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from sandbox.tests.test_storage import scope
from sandbox.tests.test_chat_session import raw_answer


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=SQLiteChatStore.create(self.root/'source.db');self.h=self.store.activate(scope())
        self.store.migrate_learning_memory();migrate_task_memory(self.store);migrate_lifecycle(self.store)
        self.repo=TaskRepository(self.store);self.repo.create(self.h,'A','任务A');self.repo.create(self.h,'B','任务B')
        self.a=self.repo.bind(self.h,'A');self.b=self.repo.bind(self.h,'B');self.life=TaskLifecycle(self.store)
        self.admin=LocalMaintenance(self.store,self.root/'maintenance',{'min_free_bytes':0})

    def tearDown(self):self.temp.cleanup()

    def edit(self,kind,text,rid='edit',revision=0):
        self.a.edit_learning(self.h,kind,text,expected_revision=revision,request_id=rid)

    def toggle(self,target,op='operation'):
        p=self.life.preview(self.h,'A',target)
        return self.life.apply(self.h,'A',target,preview_token=p['preview_token'],operation_id=op)

    def ask(self,rid='question',diagnostics=None):
        q=CourseQuestion(request_id=rid,question='QUESTION_PRIVATE_CANARY',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
        return answer_chat(q,store=self.a,handle=self.h,config_path='unused',state_dir=self.root,diagnostics=diagnostics)

    def test_explicit_migration_preserves_task_and_refuses_pending(self):
        p=self.root/'v3.db';s=SQLiteChatStore.create(p);h=s.activate(scope());s.migrate_learning_memory();migrate_task_memory(s)
        repo=TaskRepository(s);repo.create(h,'X','旧任务');x=repo.bind(h,'X');x.begin(h,'pending','等待')
        with self.assertRaises(StorageError):migrate_lifecycle(s)
        with sqlite3.connect(p) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
        x.finish(h,'pending',error_code='interrupted');migrate_lifecycle(s);migrate_lifecycle(s)
        self.assertFalse(repo.list(h)['tasks'][0]['archived']);self.assertEqual(repo.list(h)['tasks'][0]['title'],'旧任务')

    def test_archived_prompt_blocked_but_management_view_preserved(self):
        self.edit('goal','ARCHIVE_CANARY');self.toggle('archived')
        self.assertEqual(self.a.learning_items(self.h),[])
        self.assertEqual(self.a.inspect(self.h)[0]['text'],'ARCHIVE_CANARY')
        with patch('dsh_tutor.runtime.run_prompt') as run:r=self.ask()
        self.assertEqual(r.error_code,'task_archived');run.assert_not_called()
        view=TaskMemoryAPI(self.repo).execute(self.h,dict(action='view',task_id='A'))
        self.assertEqual(view['brief'],'');self.assertTrue(view['task']['archived'])

    def test_restore_keeps_revoked_and_needs_review_and_shared_preference(self):
        self.edit('goal','旧目标');self.edit('attempt','刷新','attempt');self.edit('open_issue','空白','issue');self.edit('preference','中文','pref')
        self.a.forget_learning(self.h,'open_issue',expected_revision=1)
        self.edit('goal','新目标','goal2',1)
        self.toggle('archived','archive');self.assertEqual([x.text for x in self.b.learning_items(self.h)],['中文'])
        self.toggle('active','restore')
        rows={x['kind']:x for x in self.a.inspect(self.h)}
        self.assertEqual(rows['attempt']['state'],'needs_review');self.assertEqual(rows['open_issue']['state'],'revoked')
        self.assertEqual({x.kind for x in self.a.learning_items(self.h)},{'goal','preference'})

    def test_restore_does_not_revalidate_old_environment(self):
        self.edit('attempt','刷新')
        self.h=self.store.activate(scope(instance_generation='g2'),expected_revision=1);self.a=self.repo.bind(self.h,'A')
        self.toggle('archived','archive');self.toggle('active','restore')
        self.assertEqual(self.a.inspect(self.h)[0]['state'],'stale_environment')

    def test_stale_preview_does_not_archive_changed_memory(self):
        preview=self.life.preview(self.h,'A','archived');self.edit('goal','新目标')
        with self.assertRaises(StorageError) as e:self.life.apply(self.h,'A','archived',preview_token=preview['preview_token'],operation_id='stale')
        self.assertEqual(e.exception.code,'preview_changed');self.assertFalse(self.a.task_info(self.h)['archived'])

    def test_pending_blocks_archive_without_cancelling_or_losing_answer(self):
        self.a.begin(self.h,'pending','等待')
        with self.assertRaises(StorageError) as e:self.toggle('archived')
        self.assertEqual(e.exception.code,'task_busy')
        self.a.finish(self.h,'pending',answer='完成回答');self.toggle('archived')
        self.assertEqual(self.a.read_page(self.h).turns[0].answer,'完成回答')

    def test_archive_and_request_accept_are_serialized(self):
        barrier=Barrier(2);preview=self.life.preview(self.h,'A','archived')
        def archive():
            barrier.wait()
            try:return self.life.apply(self.h,'A','archived',preview_token=preview['preview_token'],operation_id='race')
            except StorageError as e:return e.code
        def accept():
            barrier.wait()
            try:return self.a.accept(self.h,'pending','等待').created
            except StorageError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(archive);second=pool.submit(accept);results=[first.result(),second.result()]
        archived=self.a.task_info(self.h)['archived']
        with self.store._connection() as db:pending=db.execute("SELECT COUNT(*) FROM turns WHERE status='pending'").fetchone()[0]
        self.assertFalse(archived and pending);self.assertTrue(archived or pending)

    def test_operation_replay_and_conflict_no_double_version_change(self):
        p=self.life.preview(self.h,'A','archived')
        one=self.life.apply(self.h,'A','archived',preview_token=p['preview_token'],operation_id='one')
        two=self.life.apply(self.h,'A','archived',preview_token=p['preview_token'],operation_id='one')
        self.assertTrue(two['replayed']);self.assertEqual(one['lifecycle_revision'],two['lifecycle_revision'])
        with self.assertRaises(StorageError):self.life.apply(self.h,'A','active',preview_token=p['preview_token'],operation_id='one')

    def test_foreign_student_and_admin_actions_not_accepted_by_student_api(self):
        other=self.h.model_copy(update={'scope':scope(student_id='other')})
        with self.assertRaises(StorageError):self.life.preview(other,'A','archived')
        api=TaskMemoryAPI(self.repo)
        for action in ('backup','restore_database','capacity','diagnostics'):
            with self.assertRaises(StorageError):api.execute(self.h,dict(action=action))

    def test_archive_limit_and_archived_edit_denied(self):
        life=TaskLifecycle(self.store,{'max_operations_per_context':1});p=life.preview(self.h,'A','archived')
        life.apply(self.h,'A','archived',preview_token=p['preview_token'],operation_id='once')
        with self.assertRaises(StorageError):self.edit('goal','禁止写')
        p=life.preview(self.h,'A','active')
        with self.assertRaises(StorageError):life.apply(self.h,'A','active',preview_token=p['preview_token'],operation_id='twice')

    def test_diagnostic_success_all_stage_times_and_no_plaintext(self):
        diag=DiagnosticStore(self.store)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:r=self.ask(diagnostics=diag)
        self.assertEqual(r.state,'saved');self.assertEqual(run.call_count,1)
        result=diag.read_page(self.h);self.assertEqual(len(result['events']),1)
        event=result['events'][0];self.assertEqual([x['stage'] for x in event['timeline']],['input','accept','history','prepare','revalidate','model','result','save'])
        self.assertNotIn('QUESTION_PRIVATE_CANARY',json.dumps(result));self.assertNotIn('question',event['request_ref'])
        self.assertGreaterEqual(event['elapsed_seconds'],0)

    def test_diagnostic_failure_points_to_model_without_exception_body(self):
        diag=DiagnosticStore(self.store)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeError('SECRET_EXCEPTION_CANARY')):r=self.ask(diagnostics=diag)
        event=diag.read_page(self.h)['events'][0]
        self.assertEqual(event['stage'],'model');self.assertEqual(event['error_code'],'run_failed')
        self.assertNotIn('SECRET_EXCEPTION_CANARY',json.dumps(event));self.assertEqual(r.state,'failed')

    def test_diagnostic_write_failure_does_not_repeat_or_hide_saved_answer(self):
        diag=DiagnosticStore(self.store)
        with patch.object(diag,'record',side_effect=StorageError('capacity')):
            with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:r=self.ask(diagnostics=diag)
        self.assertEqual(r.state,'saved');self.assertEqual(r.diagnostic_issue,'diagnostic_unavailable');self.assertEqual(run.call_count,1)

    def test_diagnostics_bounded_paged_and_scope_protected(self):
        diag=DiagnosticStore(self.store,{'max_events':3,'page_limit':2})
        for i in range(5):
            with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):self.ask(str(i),diagnostics=diag)
        result=diag.read_page(self.h);self.assertEqual(len(result['events']),2);self.assertIsNotNone(result['next_before'])
        self.assertEqual(len(diag.read_page(self.h,before=result['next_before'])['events']),1)
        with self.assertRaises(StorageError):diag.read_page(self.h,limit=3)
        with self.assertRaises(StorageError):diag.read_page(self.h.model_copy(update={'scope':scope(student_id='other')}))
        self.assertEqual(self.admin.capacity()['counts']['diagnostic_events'],3)

    def test_backup_restores_data_and_preserves_live_database(self):
        self.edit('goal','BACKUP_CANARY');before=self.store.path.read_bytes()
        backup=self.admin.create_backup();report=self.admin.validate_restore(backup['backup_id'])
        self.assertEqual(self.store.path.read_bytes(),before);self.assertFalse(report['chat_enabled'])
        copy=self.admin.root/'validations'/report['validation_id']/'snapshot.sqlite'
        with sqlite3.connect(copy) as db:
            self.assertEqual(db.execute('SELECT quote FROM task_learning_memory').fetchone()[0],'BACKUP_CANARY')
            self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0],VALIDATION_ID)
        with self.assertRaises(StorageError):SQLiteChatStore(copy)
        moved=self.root/'relocated.sqlite';shutil.copyfile(copy,moved)
        with self.assertRaises(StorageError):SQLiteChatStore(moved)

    def test_backup_is_consistent_with_wal_and_pending_not_retried(self):
        with sqlite3.connect(self.store.path) as db:db.execute('PRAGMA journal_mode=WAL')
        self.a.begin(self.h,'pending','保留未完成')
        backup=self.admin.create_backup();report=self.admin.validate_restore(backup['backup_id'])
        self.assertEqual(report['restored_stats']['pending'],1)
        self.assertEqual(self.a.read_page(self.h).turns[0].status,'pending')

    def test_old_backup_cannot_resurrect_later_revoked_memory_in_chat(self):
        self.edit('goal','OLD_SECRET');backup=self.admin.create_backup()
        self.a.forget_learning(self.h,'goal',expected_revision=1)
        report=self.admin.validate_restore(backup['backup_id'])
        self.assertFalse(report['latest_revocations_reconciled']);self.assertFalse(report['chat_enabled'])
        self.assertEqual(self.a.learning_items(self.h),[])

    def test_backup_corruption_detected_before_restore(self):
        backup=self.admin.create_backup();path=self.admin.root/'backups'/(backup['backup_id']+'.sqlite')
        with path.open('ab') as f:f.write(b'corrupt')
        with self.assertRaises(StorageError) as e:self.admin.validate_restore(backup['backup_id'])
        self.assertEqual(e.exception.code,'backup_corrupt');self.assertEqual(list((self.admin.root/'validations').iterdir()),[])

    def test_backup_manifest_wrong_type_and_path_traversal_rejected(self):
        backup=self.admin.create_backup();manifest=self.admin.root/'backups'/(backup['backup_id']+'.json');manifest.write_text('[]')
        with self.assertRaises(StorageError):self.admin.validate_restore(backup['backup_id'])
        with self.assertRaises(StorageError):self.admin.validate_restore('../source.db')

    def test_backup_disk_full_timeout_caps_and_lock_release(self):
        with patch('dsh_tutor.maintenance.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaises(StorageError) as e:self.admin.create_backup()
        self.assertEqual(e.exception.code,'disk_low')
        with patch.object(self.admin,'_progress',return_value=lambda *args:(_ for _ in ()).throw(StorageError('maintenance_timeout'))):
            with self.assertRaises(StorageError):self.admin.create_backup()
        self.assertFalse((self.admin.root/'.maintenance.lock').exists());self.assertEqual(list((self.admin.root/'backups').iterdir()),[])
        capped=LocalMaintenance(self.store,self.root/'small',{'max_backup_bytes':65536,'min_free_bytes':0})
        with self.assertRaises(StorageError):capped.create_backup()

    def test_single_job_lock_never_stolen(self):
        with self.admin._job():
            with self.assertRaises(StorageError) as e:self.admin.create_backup()
            self.assertEqual(e.exception.code,'maintenance_busy');self.assertTrue((self.admin.root/'.maintenance.lock').exists())
        self.assertFalse((self.admin.root/'.maintenance.lock').exists())

    def test_backup_count_and_validation_limits_do_not_delete_existing(self):
        admin=LocalMaintenance(self.store,self.root/'limited',{'max_backup_files':1,'max_validation_runs':1,'min_free_bytes':0})
        backup=admin.create_backup();admin.validate_restore(backup['backup_id'])
        with self.assertRaises(StorageError):admin.create_backup()
        with self.assertRaises(StorageError):admin.validate_restore(backup['backup_id'])
        self.assertTrue((admin.root/'backups'/(backup['backup_id']+'.sqlite')).exists())

    def test_uuid_collision_does_not_remove_existing_backup(self):
        backup=self.admin.create_backup();path=self.admin.root/'backups'/(backup['backup_id']+'.sqlite');before=path.read_bytes()
        with patch('dsh_tutor.maintenance.uuid4',return_value=SimpleNamespace(hex=backup['backup_id'])):
            with self.assertRaises(StorageError):self.admin.create_backup()
        self.assertEqual(path.read_bytes(),before)

    def test_capacity_snapshot_no_growth_guess_or_automatic_cleanup(self):
        self.edit('goal','PRIVATE_CAPACITY_CANARY');self.toggle('archived')
        result=self.admin.capacity()
        self.assertEqual(result['counts']['archived_tasks'],1);self.assertGreater(result['bytes']['database'],0)
        self.assertIsNone(result['growth_rate']);self.assertFalse(result['automatic_actions'])
        self.assertNotIn('PRIVATE_CAPACITY_CANARY',json.dumps(result))

    def test_unexpected_program_error_records_stage_and_reraises_without_body_log(self):
        diag=DiagnosticStore(self.store)
        with patch('dsh_tutor.task_memory.TaskMemoryStore.current',side_effect=RuntimeError('PRIVATE_BUG_CANARY')):
            with self.assertRaises(RuntimeError):self.ask(diagnostics=diag)
        event=diag.read_page(self.h)['events'][0]
        self.assertEqual(event['stage'],'prepare');self.assertEqual(event['error_code'],'unexpected_failure')
        self.assertNotIn('PRIVATE_BUG_CANARY',json.dumps(event))

    def test_resource_entry_can_share_diagnostics_without_extra_model(self):
        from dsh_tutor.resources import ResourceManagedTutor
        diag=DiagnosticStore(self.store)
        service=ResourceManagedTutor(policy={},config_path='unused',state_dir=self.root,diagnostics=diag)
        q=CourseQuestion(request_id='managed',question='继续',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:r=service.answer(q,store=self.a,handle=self.h)
        self.assertEqual(r.chat.state,'saved');self.assertEqual(run.call_count,1);self.assertEqual(len(diag.read_page(self.h)['events']),1)

    def test_quarantine_marker_blocks_even_normal_id_during_restore(self):
        target=self.root/'partial';target.mkdir();(target/'.validation-only').write_text('验证中')
        shutil.copyfile(self.store.path,target/'snapshot.sqlite')
        with self.assertRaises(StorageError) as e:SQLiteChatStore(target/'snapshot.sqlite')
        self.assertEqual(e.exception.code,'validation_only')

    def test_io_failure_reports_stable_code_and_releases_maintenance_lock(self):
        with patch.object(self.admin,'_space',side_effect=OSError('PRIVATE_PATH_CANARY')):
            with self.assertRaises(StorageError) as e:self.admin.create_backup()
        self.assertEqual(e.exception.code,'maintenance_failed');self.assertFalse((self.admin.root/'.maintenance.lock').exists())

    def test_lifecycle_management_http_preview_archive_restore(self):
        from http.client import HTTPConnection
        from threading import Thread
        import re
        from sandbox.task_memory_demo import create_server
        server=create_server(self.store,self.h,port=0);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        def call(method,body=None,token=None):
            c=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            try:
                headers={'Content-Type':'application/json','X-Demo-Token':token or ''}
                c.request(method,'/' if method=='GET' else '/api',body=json.dumps(body) if body is not None else None,headers=headers)
                r=c.getresponse();return r.status,r.read().decode()
            finally:c.close()
        try:
            _,page=call('GET');token=json.loads(re.search(r'const token=("[^"]+")',page).group(1))
            for i,target in enumerate(('archived','active')):
                status,body=call('POST',dict(action='preview_lifecycle',task_id='A',target=target),token)
                self.assertEqual(status,200);preview=json.loads(body)
                status,body=call('POST',dict(action='apply_lifecycle',task_id='A',target=target,preview_token=preview['preview_token'],operation_id=str(i)),token)
                self.assertEqual(status,200);self.assertEqual(json.loads(body)['archived'],target=='archived')
        finally:server.shutdown();server.server_close();thread.join(5)

    def test_slow_final_hash_does_not_publish_backup_after_deadline(self):
        import time
        from dsh_tutor.maintenance import _digest
        admin=LocalMaintenance(self.store,self.root/'slow',{'deadline_seconds':0.05,'min_free_bytes':0})
        def slow_hash(path):time.sleep(0.06);return _digest(path)
        with patch('dsh_tutor.maintenance._digest',side_effect=slow_hash):
            with self.assertRaises(StorageError) as e:admin.create_backup()
        self.assertEqual(e.exception.code,'maintenance_timeout');self.assertEqual(list((admin.root/'backups').iterdir()),[])


if __name__=='__main__':unittest.main()
