"""只用虚构内容与临时数据库；验证持久化、范围、事务、错误和资源上限，不调用模型。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from dsh_tutor.conversation import ConversationScope
from dsh_tutor.storage import SQLiteChatStore,StorageLimits,StorageError


def scope(**updates):
    values=dict(student_id='student-a',learning_context_id='enrollment-a',course_id='course-a',
                course_version='v1',lesson_id='l1',instance_id='vm-a',instance_generation='g1',environment_os='linux')
    values.update(updates)
    return ConversationScope.model_validate(values)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'chat.sqlite3'
        self.store=SQLiteChatStore.create(self.path)
        self.handle=self.store.activate(scope())

    def tearDown(self):
        self.temp.cleanup()

    def assert_code(self,code,call):
        with self.assertRaises(StorageError) as caught:
            call()
        self.assertEqual(caught.exception.code,code)

    def done(self,rid='r1',question='问题',answer='最终回答',handle=None):
        handle=handle or self.handle
        self.store.begin(handle,rid,question)
        return self.store.finish(handle,rid,answer=answer,run_id='run-'+rid)

    def test_reopen_restores_exact_records_and_partial_quality(self):
        self.store.begin(self.handle,'r1','引号\'与中文\n换行')
        result=self.store.finish(self.handle,'r1',answer='部分回答',answer_status='partial',run_id='run-1')
        reopened=SQLiteChatStore(self.path)
        h=reopened.current(student_id='student-a',learning_context_id='enrollment-a')
        self.assertEqual(h,self.handle)
        self.assertEqual(reopened.read_page(h).turns,[result])
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.answer_status,'partial')

    def test_fresh_process_restores_committed_turn(self):
        self.done(answer='PERSISTENCE_CANARY')
        code="from dsh_tutor.storage import SQLiteChatStore; import sys; s=SQLiteChatStore(sys.argv[1]); h=s.current(student_id='student-a',learning_context_id='enrollment-a'); assert s.read_page(h).turns[0].answer=='PERSISTENCE_CANARY'"
        result=subprocess.run([sys.executable,'-c',code,str(self.path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_existing_file_is_never_overwritten(self):
        original=self.path.read_bytes()
        self.assert_code('already_exists',lambda: SQLiteChatStore.create(self.path))
        self.assertEqual(self.path.read_bytes(),original)

    def test_missing_database_not_silently_created(self):
        missing=Path(self.temp.name)/'missing.db'
        self.assert_code('unavailable',lambda: SQLiteChatStore(missing))
        self.assertFalse(missing.exists())

    def test_other_student_read_write_delete_and_takeover_denied(self):
        self.done(answer='PRIVATE_CANARY')
        foreign=self.handle.model_copy(update={'scope':scope(student_id='student-b')})
        for call in [lambda:self.store.read_page(foreign),lambda:self.store.begin(foreign,'r2','x'),
                     lambda:self.store.finish(foreign,'r1',answer='x'),lambda:self.store.delete_history(foreign),
                     lambda:self.store.activate(scope(student_id='student-b'),expected_revision=1),
                     lambda:self.store.current(student_id='student-b',learning_context_id='enrollment-a')]:
            self.assert_code('scope_denied',call)
        self.assertEqual(self.store.read_page(self.handle).turns[0].answer,'PRIVATE_CANARY')

    def test_distinct_students_and_contexts_are_independent(self):
        other=self.store.activate(scope(student_id='student-b',learning_context_id='enrollment-b'))
        self.done(answer='A')
        self.done(handle=other,answer='B')
        self.assertEqual(self.store.read_page(self.handle).turns[0].answer,'A')
        self.assertEqual(self.store.read_page(other).turns[0].answer,'B')

    def test_all_scope_fields_checked_even_with_valid_revision(self):
        for field,value in [('course_id','b'),('course_version','v2'),('lesson_id','l2'),('instance_id','vm-b'),
                            ('instance_generation','g2'),('environment_os','windows')]:
            forged=self.handle.model_copy(update={'scope':scope(**{field:value})})
            self.assert_code('scope_denied',lambda:self.store.read_page(forged))

    def test_generation_change_cancels_pending_and_rejects_late_answer(self):
        self.store.begin(self.handle,'old','旧问题')
        new=self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        self.assertEqual(new.revision,2)
        self.assert_code('stale_scope',lambda:self.store.finish(self.handle,'old',answer='晚到回答'))
        old=self.store.read_page(self.handle).turns[0]
        self.assertEqual((old.status,old.error_code),('cancelled','scope_changed'))
        self.assertEqual(self.store.read_page(new).turns,[])
        self.done('new',handle=new)

    def test_switch_uses_compare_and_swap_and_repeat_same_scope_is_noop(self):
        self.assertEqual(self.store.activate(scope(),expected_revision=1),self.handle)
        self.assert_code('revision_conflict',lambda:self.store.activate(scope(lesson_id='l2'),expected_revision=0))
        self.store.activate(scope(lesson_id='l2'),expected_revision=1)
        self.assert_code('revision_conflict',lambda:self.store.activate(scope(lesson_id='l3'),expected_revision=1))

    def test_duplicate_begin_and_finish_are_idempotent_without_new_sequence(self):
        a=self.store.begin(self.handle,'r1','问题')
        b=self.store.begin(self.handle,'r1','问题')
        self.assertEqual(a,b)
        final=self.store.finish(self.handle,'r1',answer='回答')
        self.assertEqual(final,self.store.finish(self.handle,'r1',answer='回答'))
        self.assertEqual(final,self.store.begin(self.handle,'r1','问题'))
        self.assertEqual(len(self.store.read_page(self.handle).turns),1)

    def test_request_conflicts_and_terminal_overwrite_rejected(self):
        self.done()
        self.assert_code('request_conflict',lambda:self.store.begin(self.handle,'r1','不同问题'))
        self.assert_code('terminal_conflict',lambda:self.store.finish(self.handle,'r1',answer='改写'))
        self.assert_code('request_not_found',lambda:self.store.finish(self.handle,'missing',answer='x'))

    def test_only_one_pending_request_per_learning_context(self):
        self.store.begin(self.handle,'r1','先问')
        self.assert_code('request_in_progress',lambda:self.store.begin(self.handle,'r2','后问'))
        self.store.finish(self.handle,'r1',error_code='model_failed')
        self.assertEqual(self.store.begin(self.handle,'r2','后问').status,'pending')

    def test_concurrent_duplicate_requests_create_one_record(self):
        def begin(_):
            return self.store.begin(self.handle,'same','同一问题').sequence
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(begin,range(4)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(len(self.store.read_page(self.handle).turns),1)

    def test_concurrent_different_questions_have_one_winner(self):
        def begin(rid):
            try:
                self.store.begin(self.handle,rid,'问题')
                return 'accepted'
            except StorageError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(begin,['r1','r2']))
        self.assertEqual(sorted(result),['accepted','request_in_progress'])

    def test_process_exit_retains_pending_without_faking_completion(self):
        code="from dsh_tutor.storage import SQLiteChatStore; import sys,os; s=SQLiteChatStore(sys.argv[1]); h=s.current(student_id='student-a',learning_context_id='enrollment-a'); s.begin(h,'crash','未完成问题'); os._exit(9)"
        result=subprocess.run([sys.executable,'-c',code,str(self.path)],timeout=10)
        self.assertEqual(result.returncode,9)
        row=SQLiteChatStore(self.path).read_page(self.handle).turns[0]
        self.assertEqual((row.status,row.answer),('pending',None))
        self.store.finish(self.handle,'crash',error_code='interrupted')
        self.assertEqual(self.store.read_page(self.handle).turns[0].status,'failed')

    def test_uncommitted_process_exit_rolls_back_partial_write(self):
        self.done()
        code="import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE turns SET answer='UNCOMMITTED'\"); os._exit(8)"
        result=subprocess.run([sys.executable,'-c',code,str(self.path)],timeout=10)
        self.assertEqual(result.returncode,8)
        self.assertEqual(self.store.read_page(self.handle).turns[0].answer,'最终回答')

    def test_page_cursor_is_bounded_ordered_and_has_no_duplicates(self):
        for i in range(7):self.done(str(i))
        cursor=None;seen=[]
        while True:
            page=self.store.read_page(self.handle,before=cursor,limit=2)
            ids=[x.sequence for x in page.turns]
            self.assertEqual(ids,sorted(ids));self.assertLessEqual(len(ids),2)
            seen+=ids
            if page.next_before is None:break
            cursor=page.next_before
        self.assertEqual(len(seen),7);self.assertEqual(len(set(seen)),7)
        self.assert_code('invalid_input',lambda:self.store.read_page(self.handle,limit=101))

    def test_delete_hides_text_and_late_retries_cannot_resurrect_it(self):
        self.done(question='SECRET_QUESTION',answer='SECRET_ANSWER')
        self.assertEqual(self.store.delete_history(self.handle),1)
        self.assertEqual(self.store.delete_history(self.handle),0)
        self.assertEqual(self.store.read_page(self.handle).turns,[])
        self.assert_code('request_conflict',lambda:self.store.begin(self.handle,'r1','SECRET_QUESTION'))
        self.assert_code('terminal_conflict',lambda:self.store.finish(self.handle,'r1',answer='SECRET_ANSWER'))
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT question,answer,run_id FROM turns').fetchone()
            self.assertEqual(row,('',None,None))

    def test_delete_old_scope_does_not_delete_new_scope(self):
        self.done()
        new=self.store.activate(scope(lesson_id='l2'),expected_revision=1)
        self.done('r2',handle=new,answer='新课时')
        self.store.delete_history(self.handle)
        self.assertEqual(self.store.read_page(new).turns[0].answer,'新课时')

    def test_pending_deletion_blocks_late_reply_and_allows_new_request(self):
        self.store.begin(self.handle,'pending','待删除')
        self.store.delete_history(self.handle)
        self.assert_code('terminal_conflict',lambda:self.store.finish(self.handle,'pending',answer='晚到'))
        self.assertEqual(self.store.begin(self.handle,'new','新问题').status,'pending')

    def test_old_request_id_cannot_be_reused_in_new_scope(self):
        self.done()
        new=self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        self.assert_code('request_conflict',lambda:self.store.begin(new,'r1','问题'))
        self.assertEqual(self.store.read_page(new).turns,[])

    def test_concurrent_scope_switch_and_reply_never_write_to_new_scope(self):
        from threading import Barrier
        gate=Barrier(2)
        self.store.begin(self.handle,'old','旧问题')
        def finish():
            gate.wait()
            try:
                return self.store.finish(self.handle,'old',answer='旧回答').status
            except StorageError as exc:
                return exc.code
        def switch():
            gate.wait()
            return self.store.activate(scope(instance_generation='g2'),expected_revision=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(finish);b=pool.submit(switch)
            outcome,new=a.result(),b.result()
        self.assertIn(outcome,['completed','stale_scope'])
        self.assertEqual(self.store.read_page(new).turns,[])
        old=self.store.read_page(self.handle).turns[0]
        self.assertEqual(old.status,'completed' if outcome=='completed' else 'cancelled')

    def test_sql_injection_text_is_only_data(self):
        text="'); DROP TABLE turns; --"
        self.done(question=text,answer=text)
        self.assertEqual(self.store.read_page(self.handle).turns[0].question,text)

    def test_invalid_input_does_not_complete_or_leak_private_text(self):
        self.store.begin(self.handle,'r1','PRIVATE')
        for args in [dict(answer='A'*16001),dict(answer=''),dict(error_code='PRIVATE_EXCEPTION'),
                     dict(answer='x',error_code='timeout'),dict(answer='x',answer_status='invented')]:
            self.assert_code('invalid_input',lambda:self.store.finish(self.handle,'r1',**args))
        self.assertEqual(self.store.read_page(self.handle).turns[0].status,'pending')
        malformed=scope().model_copy(update={'instance_generation':None})
        self.assert_code('invalid_scope',lambda:self.store.activate(malformed,expected_revision=1))

    def test_lock_wait_is_bounded_and_reports_busy(self):
        store=SQLiteChatStore(self.path,limits={'busy_timeout_ms':50})
        with closing(sqlite3.connect(self.path,isolation_level=None)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            start=time.monotonic()
            self.assert_code('busy',lambda:store.begin(self.handle,'locked','问题'))
            self.assertLess(time.monotonic()-start,1)
            lock.rollback()
        self.done()

    def test_turn_and_context_caps_fail_without_deleting_records(self):
        store=SQLiteChatStore(self.path,limits={'max_turns_per_context':1,'max_contexts':1})
        self.done()
        self.assert_code('capacity',lambda:store.begin(self.handle,'r2','问题'))
        self.assert_code('capacity',lambda:store.activate(scope(learning_context_id='other')))
        self.assertEqual(len(store.read_page(self.handle).turns),1)

    def test_database_page_cap_rolls_back_full_write(self):
        store=SQLiteChatStore(self.path,limits={'max_db_bytes':65536})
        error=None
        for i in range(20):
            try:
                store.begin(self.handle,str(i),'Q'*2000)
                store.finish(self.handle,str(i),answer='A'*16000)
            except StorageError as exc:
                error=exc.code;break
        self.assertEqual(error,'capacity')
        self.assertLessEqual(self.path.stat().st_size,65536)
        # 容量错误之后数据库仍可读取，不伪装写入成功。
        self.assertTrue(store.read_page(self.handle).turns)

    def test_corruption_wrong_schema_and_symlink_rejected(self):
        bad=Path(self.temp.name)/'corrupt.db';bad.write_text('not sqlite data')
        self.assert_code('corrupt',lambda:SQLiteChatStore(bad))
        wrong=Path(self.temp.name)/'other.db'
        with closing(sqlite3.connect(wrong)) as db:db.execute('CREATE TABLE user_data(value TEXT)')
        self.assert_code('unsupported_schema',lambda:SQLiteChatStore(wrong))
        link=Path(self.temp.name)/'link.db';link.symlink_to(self.path)
        self.assert_code('unavailable',lambda:SQLiteChatStore(link))

    def test_readonly_write_failure_is_explicit_and_record_survives(self):
        self.done()
        self.path.chmod(0o400)
        try:
            self.assert_code('unavailable',lambda:self.store.begin(self.handle,'r2','问题'))
        finally:
            self.path.chmod(0o600)
        self.assertEqual(self.store.read_page(self.handle).turns[0].answer,'最终回答')

    def test_no_model_history_loading_side_effect(self):
        from unittest.mock import patch
        with patch('dsh_tutor.runtime.run_prompt') as run:
            self.done()
            self.store.read_page(self.handle)
            run.assert_not_called()


if __name__=='__main__':unittest.main()
