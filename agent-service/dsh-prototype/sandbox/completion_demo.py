"""阶段3有限演示：实际DSH到假提供方，慢交付重叠、有效期和安全清理均为虚构数据。"""
from dataclasses import replace
from threading import Event,Lock
from pathlib import Path
from uuid import uuid4
import time
from sandbox.efficiency_demo import EfficiencyDemo
from sandbox.integration_client import ClientError
from dsh_tutor.operation_retention import RequestWindow
from dsh_tutor.agent_service import binding_refs
from dsh_tutor.run_registry import RunLease
from dsh_tutor.service_maintenance import ServiceMaintenance
from dsh_tutor.maintenance import LocalMaintenance

class CompletionDemo(EfficiencyDemo):
    def __init__(self,project):
        super().__init__(project);self.first_saving=Event();self.release_first=Event();self._first=True;self._delivery_lock=Lock();self.observed={}
        self.fixture.journal.enable_retention()
        original=self.fixture.journal.finish
        def slow(op,instance,**kw):
            hold=False
            with self._delivery_lock:
                if self._first and kw.get('state')=='succeeded':self._first=False;hold=True
            if hold:
                self.first_saving.set()
                if not self.release_first.wait(15):raise RuntimeError('simulation_save_timeout')
            return original(op,instance,**kw)
        self.fixture.journal.finish=slow
        maintenance=LocalMaintenance(self.fixture.store,self.root/'maintenance',{'min_free_bytes':0})
        self.maintenance=ServiceMaintenance(self.service,runtime_root=self.root,chat_path=self.database,
            local_maintenance=maintenance,cleanup_limits={'min_age_seconds':0.0})
    def _traffic(self):
        try:
            a=self.client();_,first=a.submit(self.fixture.request('complete-A'))
            if not self.first_saving.wait(8):raise RuntimeError('first_not_saving')
            snapshot=self.fixture.tutor.gate.snapshot();view=a.view(first['operation_id'])
            self.observed.update(first_pending=view['state']!='succeeded',active_during_first_save=snapshot['active'],finishing_during_first_save=snapshot['finishing_reserved'],
                first_stage=view['stage'],first_elapsed=view['feedback']['elapsed_seconds'],eta=view['feedback']['estimated_remaining_seconds'],
                busy_maintenance=self.maintenance.run_once('snapshot')['state'])
            b=self.client('B');_,second=b.submit(self.fixture.request('complete-B'));self.results['B']=self.wait(b,second['operation_id'])
            self.observed['second_saved_before_first']=not self.release_first.is_set() and self.results['B']['state']=='succeeded'
            self.release_first.set();self.results['A']=self.wait(a,first['operation_id'])
            deadline=time.monotonic()+3
            while self.service.metrics()['active_operations'] and time.monotonic()<deadline:time.sleep(.02)
            # 已过期操作为手工编制的虚构保留样例，不模拟一次真实模型运行。
            at=int(time.time());binding=self.fixture.bindings['C'];window=RequestWindow(nonce='b'*32,issued_at=at-10,expires_at=at-1)
            binding=replace(binding,request_window=window);self.fixture.bindings['C']=binding;owner,scope=binding_refs(binding);wid=window.identifier(owner);op=uuid4().hex
            with self.fixture.journal.connection(write=True) as db:
                db.execute('INSERT INTO request_windows VALUES(?,?,?,?)',(wid,owner,window.issued_at,window.expires_at))
                db.execute("INSERT INTO operations(operation_id,owner_ref,scope_ref,request_id,digest,profile,instance_id,state,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'failed','done','2000','2000')",(op,owner,scope,wid+'.old','d','course_snapshot','fixture'))
                db.execute('INSERT INTO operation_windows VALUES(?,?)',(op,wid))
            plan=self.maintenance.run_once('retention',retention_limits={'expiry_grace_seconds':0.0});self.observed['retention']=self.maintenance.run_once('retention',preview=False,expected=plan['token'],retention_limits={'expiry_grace_seconds':0.0})
            raw=self.fixture.request(wid+'.old');raw['request_window_id']=wid
            try:self.client('C').submit(raw);raise RuntimeError('expired_accepted')
            except ClientError as exc:self.observed['expired_http_status']=exc.status
            # 仅清理新建的一次性小样例，保留本轮DSH元数据供复核。
            p=self.root/uuid4().hex;p.mkdir();lease=RunLease(p,'cleanup_fixture');(p/'fixture.txt').write_text('disposable');lease.close('completed')
            unknown=self.root/uuid4().hex;unknown.mkdir();lease=RunLease(unknown,'unknown_fixture');lease.close('unknown')
            plan=self.maintenance.run_once('run_cleanup');selected=[r for r in plan['items'] if r['run_id']==p.name]
            self.observed['cleanup']=self.maintenance.run_once('run_cleanup',preview=False,items=selected)
            self.observed['unknown_preserved']=unknown.exists();self.observed['capacity']=self.maintenance.run_once('snapshot')
        except Exception:self.error_code='completion_demo_failed'
        finally:self.release_first.set();self.done.set()
