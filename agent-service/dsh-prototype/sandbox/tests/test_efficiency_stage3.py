"""阶段3：真实线程/SQLite/文件系统边界；推理用替身，不访问Mac。"""
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event,Lock
from uuid import uuid4
import json,os,sqlite3,tempfile,time,unittest
from dsh_tutor.service_contracts import ServiceError,ServiceSettings
from dsh_tutor.agent_service import binding_refs
from dsh_tutor.operation_retention import RequestWindow
from dsh_tutor.operation_journal import OperationJournal
from dsh_tutor.run_registry import RunLease,RunInventory,MARKER
from dsh_tutor.service_maintenance import ServiceMaintenance
from sandbox.integration_fixture import Fixture,fake_model
from sandbox.tests.test_resources import wait_for

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.f=Fixture(self.root/'fixture');self.addCleanup(self.f.close);self.f.service.verify_ready(upstream_idle=True)
    def wait(self,v,b=None):
        b=b or self.f.bindings['A'];deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            v=self.f.service.view(b,v['operation_id'])
            if v['state'] in ('succeeded','failed','recovery_required'):return v
            time.sleep(.01)
        self.fail('operation incomplete')
    def submit(self,rid='sample',b=None):return self.f.service.submit(b or self.f.bindings['A'],self.f.request(rid))[0]

class PipelineTests(Base):
    def test_slow_chat_save_releases_model_but_keeps_completion_and_total(self):
        entered=Event();release=Event();second=Event();finish=self.f.store.finish;calls=0
        def save(*args,**kw):
            if args[1]=='slow':entered.set();release.wait(4)
            return finish(*args,**kw)
        def model(*a,**kw):
            nonlocal calls
            calls+=1
            if calls==2:second.set()
            return fake_model(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model),patch.object(self.f.store,'finish',side_effect=save):
            a=self.submit('slow');self.assertTrue(entered.wait(2))
            try:
                snap=self.f.tutor.gate.snapshot();self.assertEqual(snap['active'],0);self.assertEqual(snap['finishing_reserved'],1);self.assertEqual(snap['inflight'],1)
                b=self.f.bindings['B'];v=self.submit('second',b);self.assertTrue(second.wait(2));self.assertEqual(self.wait(v,b)['state'],'succeeded')
                with self.assertRaises(ServiceError):self.submit('same-student')
            finally:release.set()
            self.assertEqual(self.wait(a)['state'],'succeeded')
    def test_slow_delivery_save_keeps_completion_reservation_and_is_not_delivered(self):
        entered=Event();release=Event();finish=self.f.journal.finish;first=True
        def save(*a,**kw):
            nonlocal first
            if kw.get('state')=='succeeded' and first:first=False;entered.set();release.wait(4)
            return finish(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model),patch.object(self.f.journal,'finish',side_effect=save):
            a=self.submit();self.assertTrue(entered.wait(2))
            try:
                self.assertNotEqual(self.f.service.view(self.f.bindings['A'],a['operation_id'])['state'],'succeeded')
                self.assertEqual(self.f.tutor.gate.snapshot()['active'],0);self.assertEqual(self.f.tutor.gate.snapshot()['finishing_reserved'],1)
                b=self.f.bindings['B'];self.assertEqual(self.wait(self.submit('second',b),b)['state'],'succeeded')
            finally:release.set()
            self.assertEqual(self.wait(a)['state'],'succeeded')
    def test_full_completion_stops_new_models_until_save_backlog_drains(self):
        release=Event();entered=Event();counter=0;lock=Lock();finish=self.f.store.finish
        def save(*a,**kw):
            nonlocal counter
            with lock:counter+=1;entered.set() if counter>=2 else None
            release.wait(4);return finish(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model) as model,patch.object(self.f.store,'finish',side_effect=save):
            self.submit('a');self.submit('b',self.f.bindings['B']);self.assertTrue(entered.wait(2))
            try:
                c=self.submit('c',self.f.bindings['C']);wait_for(lambda:self.f.tutor.gate.snapshot()['finishing_waiting']==1)
                self.assertEqual(model.call_count,2);self.assertEqual(self.f.tutor.gate.snapshot()['active'],0)
            finally:release.set()
            self.assertEqual(self.wait(c,self.f.bindings['C'])['state'],'succeeded')
    def test_scope_change_during_save_still_withholds_old_answer(self):
        entered=Event();release=Event();finish=self.f.store.finish;b=self.f.bindings['A']
        def save(*a,**kw):entered.set();release.wait(4);return finish(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model),patch.object(self.f.store,'finish',side_effect=save):
            v=self.submit();self.assertTrue(entered.wait(2))
            self.f.store.activate(b.handle.scope.model_copy(update={'instance_generation':'g2'}),expected_revision=b.handle.revision)
            release.set();wait_for(lambda:not self.f.service._active)
        with self.assertRaises(ServiceError):self.f.service.view(b,v['operation_id'])
    def test_repeated_progress_is_not_written_but_transition_is(self):
        j=self.f.journal;j.claim(operation_id='x',owner_ref='o',scope_ref='s',request_id='r',digest='d',profile='course_snapshot',instance='i')
        for _ in range(100):j.progress('x','i','model')
        self.assertEqual(len(j.events('x',0)['items']),2);j.progress('x','i','save');self.assertEqual(len(j.events('x',0)['items']),3)
    def test_maintenance_deferred_while_online_work_and_blocks_new_admission(self):
        entered=Event();release=Event();runs=self.root/'runs';runs.mkdir();m=ServiceMaintenance(self.f.service,runtime_root=runs,chat_path=self.f.store.path)
        def model(*a,**kw):entered.set();release.wait(4);return fake_model(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
            v=self.submit();self.assertTrue(entered.wait(2))
            try:self.assertEqual(m.run_once('snapshot')['state'],'deferred')
            finally:release.set()
            self.wait(v)
        wait_for(lambda:not self.f.service._active)
        with self.f.service.maintenance():
            with self.assertRaises(ServiceError):self.submit('during-maintenance')
        snap=m.run_once('snapshot');self.assertFalse(snap['coverage_complete']);self.assertIsNone(snap['components']['runtime_total']);self.assertIsNone(snap['components']['backups'])

    def test_model_wait_does_not_consume_post_queue_validation_budget(self):
        self.f.tutor.gate.reconfigure({'revalidation_timeout_seconds':.25},expected_revision=1)
        entered=Event();release=Event();calls=0
        def model(*a,**kw):
            nonlocal calls
            calls+=1
            if calls==1:entered.set();release.wait(4)
            return fake_model(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
            a=self.submit('first');self.assertTrue(entered.wait(2));b=self.f.bindings['B'];v=self.submit('wait',b)
            wait_for(lambda:self.f.tutor.gate.snapshot()['waiting']==1);Event().wait(.35);release.set()
            self.assertEqual(self.wait(v,b)['state'],'succeeded');self.wait(a)

    def test_offline_cli_respects_owner_lock_and_requires_preview_for_delete(self):
        import subprocess,sys
        project=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='cli-stage3-',dir=project/'sandbox/results') as directory:
            root=Path(directory);f=Fixture(root/'fixture');closed=False
            try:
                f.journal.enable_retention();p=f.root/uuid4().hex;p.mkdir();lease=RunLease(p,'cli_fixture');(p/'fixture.txt').write_text('disposable');lease.close('completed')
                config=root/'config.json';config.write_text(json.dumps({'journal':str(f.journal.path.relative_to(project)),'runtime_root':str(f.root.relative_to(project)),'cleanup':{'min_age_seconds':0.0}}))
                cmd=[sys.executable,str(project/'tools/maintain_service.py'),'--config',str(config.relative_to(project))]
                locked=subprocess.run(cmd+['--action','preview-runs'],capture_output=True,text=True,timeout=5);self.assertNotEqual(locked.returncode,0);self.assertIn('journal_owned',locked.stderr)
                f.close();closed=True;plan=root/'preview.json'
                result=subprocess.run(cmd+['--action','preview-runs','--out',str(plan.relative_to(project))],capture_output=True,text=True,timeout=5);self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(p.exists())
                result=subprocess.run(cmd+['--action','apply-runs','--plan',str(plan.relative_to(project))],capture_output=True,text=True,timeout=5);self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(p.exists())
            finally:
                if not closed:f.close()

    def test_actual_runtime_does_not_treat_unconfirmed_provider_as_normal_completion(self):
        from sandbox.tests.test_efficiency_stage1 import ActualDshBudgetTests
        from dsh_tutor import runtime
        original=runtime._metrics
        def unconfirmed(path):
            metrics=original(path);metrics['unconfirmed_calls']=1;return metrics
        with patch('dsh_tutor.runtime._metrics',side_effect=unconfirmed):
            result,error,received,stages,rows,left=ActualDshBudgetTests().run_case('text')
        self.assertIsNone(result);self.assertEqual(error.code,'run_failed');self.assertEqual(len(received),1)

class RetentionTests(Base):
    def setUp(self):super().setUp();self.f.journal.enable_retention()
    def bound(self,*,issued=None,expires=None):
        now=int(time.time());return replace(self.f.bindings['A'],request_window=RequestWindow(nonce='a'*32,issued_at=issued or now-1,expires_at=expires or now+10))
    def raw(self,b,suffix='q'):
        owner,_=binding_refs(b);id=b.request_window.identifier(owner);r=self.f.request(id+'.'+suffix);r['request_window_id']=id;return r
    def test_window_opt_in_and_prefix_are_mandatory(self):
        b=self.bound()
        with self.assertRaises(ServiceError):self.f.service.submit(b,self.f.request())
        r=self.raw(b);r['payload']['request_id']='arbitrary'
        with self.assertRaises(ServiceError):self.f.service.submit(b,r)
    def test_expired_submit_and_retimed_window_never_reexecute(self):
        b=self.bound();r=self.raw(b)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model) as model:
            v,_=self.f.service.submit(b,r);self.assertEqual(self.wait(v,b)['state'],'succeeded');wait_for(lambda:not self.f.service._active)
            at=b.request_window.expires_at+1
            with patch('dsh_tutor.operation_retention.time.time',return_value=at):
                preview=self.f.journal.retention(limits={'expiry_grace_seconds':0.0});self.f.journal.retention(preview=False,limits={'expiry_grace_seconds':0.0},expected=preview['token'])
                with self.assertRaises(ServiceError) as e:self.f.service.submit(b,r)
                self.assertEqual(e.exception.status,410)
                fresh=self.bound(issued=at,expires=at+10);fake=dict(r,request_window_id=fresh.request_window.identifier(binding_refs(fresh)[0]))
                with self.assertRaises(ServiceError):self.f.service.submit(fresh,fake)
            self.assertEqual(model.call_count,1)
    def test_legacy_result_expiry_keeps_last_dedup_key(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model) as model:
            v=self.submit();self.wait(v);wait_for(lambda:not self.f.service._active)
            with patch('dsh_tutor.operation_retention.time.time',return_value=time.time()+10):
                p=self.f.journal.retention(limits={'result_seconds':1.0});self.f.journal.retention(preview=False,limits={'result_seconds':1.0},expected=p['token'])
            replay=self.submit();self.assertEqual(replay['error_code'],'result_expired');self.assertIsNone(replay['result']);self.assertEqual(model.call_count,1)
    def test_pending_and_recovery_records_are_not_pruned(self):
        j=self.f.journal
        for op,state in [('p',None),('u','recovery_required')]:
            j.claim(operation_id=op,owner_ref='o',scope_ref='s',request_id=op,digest='d',profile='course_snapshot',instance='i')
            if state:j.finish(op,'i',state=state)
        self.assertEqual(j.retention()['rows'],0)
    def test_changed_preview_cannot_delete_an_unreviewed_result(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model):v=self.submit();self.wait(v)
        with patch('dsh_tutor.operation_retention.time.time',return_value=time.time()+10):
            p=self.f.journal.retention(limits={'result_seconds':1.0});self.f.journal.redact(v['operation_id'])
            with self.assertRaises(ServiceError):self.f.journal.retention(preview=False,limits={'result_seconds':1.0},expected=p['token'])
    def test_clock_rollback_and_external_restore_floor_fail_closed(self):
        at=time.time()+20
        with patch('dsh_tutor.operation_retention.time.time',return_value=at):
            p=self.f.journal.retention();self.f.journal.retention(preview=False,expected=p['token'])
        b=self.bound()
        with self.assertRaises(ServiceError) as e:self.f.service.submit(b,self.raw(b))
        self.assertEqual(e.exception.code,'clock_verification_required')
        settings=ServiceSettings(request_issued_floor=int(at)+1)
        self.f.journal.settings=settings
        with patch('dsh_tutor.operation_retention.time.time',return_value=at+2):
            with self.assertRaises(ServiceError):self.f.service.submit(b,self.raw(b))
    def test_over_1000_operations_reclaimed_with_window_guard_still_rejects_old_id(self):
        j=self.f.journal;j.settings=ServiceSettings(max_operations=1200,database_timeout_seconds=1.0)
        b=self.bound();owner,_=binding_refs(b);win=b.request_window;wid=win.identifier(owner)
        with j.connection(write=True) as db:
            db.execute('INSERT INTO request_windows VALUES(?,?,?,?)',(wid,owner,win.issued_at,win.expires_at))
            for i in range(1005):
                op=f'{i:032x}';rid=wid+'.'+str(i)
                db.execute("INSERT INTO operations(operation_id,owner_ref,scope_ref,request_id,digest,profile,instance_id,state,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'failed','done','2000','2000')",(op,owner,'s',rid,'d','course_snapshot','i'))
                db.execute('INSERT INTO operation_windows VALUES(?,?)',(op,wid))
        with patch('dsh_tutor.operation_retention.time.time',return_value=win.expires_at+1):
            total=0
            for _ in range(11):
                p=j.retention(limits={'expiry_grace_seconds':0.0});self.assertLessEqual(p['rows'],100);total+=p['rows'];j.retention(preview=False,limits={'expiry_grace_seconds':0.0},expected=p['token'])
            self.assertEqual(total,1005);self.assertEqual(j.counts(),{})
            with self.assertRaises(ServiceError):self.f.service.submit(b,self.raw(b,'0'))
    def test_explicit_migration_refuses_inflight_and_is_idempotent(self):
        self.f.journal.enable_retention()
        other=OperationJournal.create(self.root/'other.db')
        try:
            other.claim(operation_id='a',owner_ref='o',scope_ref='s',request_id='r',digest='d',profile='course_snapshot',instance='i')
            with self.assertRaises(ServiceError):other.enable_retention()
        finally:other.close()

    def test_window_namespace_cannot_fall_back_to_legacy_after_pruning(self):
        b=self.bound();r=self.raw(b);r.pop('request_window_id')
        with self.assertRaises(ServiceError):self.f.service.submit(self.f.bindings['A'],r)
    def test_restored_snapshot_still_rejects_expired_window_before_cached_lookup(self):
        b=self.bound();r=self.raw(b)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model):v,_=self.f.service.submit(b,r);self.wait(v,b)
        backup=self.root/'old-journal.db'
        with self.f.journal.connection() as source:
            target=sqlite3.connect(backup)
            try:source.backup(target)
            finally:target.close()
        restored=OperationJournal(backup)
        try:
            with patch('dsh_tutor.operation_retention.time.time',return_value=b.request_window.expires_at+10):
                with self.assertRaises(ServiceError) as e:restored.validate_window(b.request_window,binding_refs(b)[0],r['payload']['request_id'],r['request_window_id'])
                self.assertEqual(e.exception.status,410)
        finally:restored.close()

    def test_expiry_grace_keeps_late_delivery_query_without_reopening_submission(self):
        b=self.bound();r=self.raw(b)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model):v,_=self.f.service.submit(b,r);self.wait(v,b)
        with patch('dsh_tutor.operation_retention.time.time',return_value=b.request_window.expires_at+1):
            self.assertEqual(self.f.journal.retention()['rows'],0)
            self.assertEqual(self.f.service.view(b,v['operation_id'])['state'],'succeeded')
            with self.assertRaises(ServiceError):self.f.service.submit(b,r)

class RunCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name);self.root=self.base/'runs';self.root.mkdir()
        self.inventory=RunInventory(self.root,{'min_age_seconds':0.0})
    def new_run(self,outcome='completed',name=None):
        p=self.root/(name or uuid4().hex);p.mkdir();lease=RunLease(p,'test');(p/'payload.txt').write_text('fixture');lease.close(outcome);return p
    def test_preview_and_cleanup_remove_only_owned_known_terminal_run(self):
        p=self.new_run();outside=self.base/'outside';outside.write_text('keep');preview=self.inventory.preview();self.assertEqual(len(preview['items']),1)
        self.inventory.apply(preview['items']);self.assertFalse(p.exists());self.assertEqual(outside.read_text(),'keep')
    def test_active_unknown_and_legacy_directories_are_preserved(self):
        self.new_run('unknown');legacy=self.root/uuid4().hex;legacy.mkdir();p=self.root/uuid4().hex;p.mkdir();lease=RunLease(p,'test')
        try:self.assertEqual(self.inventory.preview()['items'],[])
        finally:lease.close('completed')
        self.assertEqual(len(self.inventory.preview()['items']),1)
    def test_symlink_and_hardlink_never_follow_or_remove_outside(self):
        p=self.new_run();outside=self.base/'secret';outside.write_text('keep');(p/'link').symlink_to(outside)
        self.assertEqual(self.inventory.preview()['items'],[]);(p/'link').unlink();os.link(outside,p/'hard')
        self.assertEqual(self.inventory.preview()['items'],[]);self.assertEqual(outside.read_text(),'keep')
    def test_changed_preview_refuses_deletion(self):
        p=self.new_run();preview=self.inventory.preview();(p/'payload.txt').write_text('changed')
        with self.assertRaises(ValueError):self.inventory.apply(preview['items'])
        self.assertTrue(p.exists())
    def test_lease_held_prevents_cleanup_even_if_marker_says_completed(self):
        p=self.root/uuid4().hex;p.mkdir();lease=RunLease(p,'test');lease.record.update(outcome='completed',finished_at=time.time());lease._save()
        try:self.assertEqual(self.inventory.preview()['items'],[])
        finally:lease.close('completed')
    def test_root_replacement_and_parent_symlink_cannot_redirect_cleanup(self):
        self.new_run();old=self.base/'old';self.root.rename(old);outside=self.base/'outside';outside.mkdir();self.root.symlink_to(outside,target_is_directory=True)
        with self.assertRaises((ValueError,OSError)):self.inventory.preview()
        self.assertEqual(list(outside.iterdir()),[])
    def test_partial_inventory_is_explicit_and_does_not_claim_total_size(self):
        self.new_run();self.new_run();inventory=RunInventory(self.root,{'max_entries':1,'min_age_seconds':0.0});p=inventory.preview()
        self.assertTrue(p['partial']);self.assertIsNone(p['total_runtime_bytes']);self.assertLessEqual(p['seen'],1)
    def test_interrupted_cleanup_leaves_marker_for_new_preview_and_retry(self):
        p=self.new_run();(p/'a.txt').write_text('a');(p/'b.txt').write_text('b');preview=self.inventory.preview();unlink=os.unlink;count=0
        def fail(*args,**kw):
            nonlocal count
            count+=1
            if count==2:raise OSError('injected')
            return unlink(*args,**kw)
        with patch('os.unlink',side_effect=fail):
            with self.assertRaises(OSError):self.inventory.apply(preview['items'])
        self.assertTrue((p/MARKER).exists());self.inventory.apply(self.inventory.preview()['items']);self.assertFalse(p.exists())
    def test_arbitrary_paths_and_malformed_markers_are_rejected(self):
        p=self.new_run();(p/MARKER).write_text('[]');self.assertEqual(self.inventory.preview()['items'],[])
        with self.assertRaises(ValueError):self.inventory.apply([{'run_id':'../outside'}])

    def test_limits_reject_large_run_and_never_delete_it(self):
        p=self.new_run();(p/'huge').write_bytes(b'x'*2000);small=RunInventory(self.root,{'max_bytes':100,'min_age_seconds':0.0});self.assertEqual(small.preview()['items'],[]);self.assertTrue(p.exists())
    def test_cache_status_does_not_trigger_disk_inventory_on_ui_refresh(self):
        f=Fixture(self.base/'fixture')
        try:
            m=ServiceMaintenance(f.service,runtime_root=self.root,chat_path=f.store.path)
            self.assertEqual(m.status()['state'],'not_collected');m.run_once('snapshot')
            with patch.object(m.runs,'preview',side_effect=AssertionError('no disk rescan')):self.assertEqual(m.status()['state'],'available')
        finally:f.close()
