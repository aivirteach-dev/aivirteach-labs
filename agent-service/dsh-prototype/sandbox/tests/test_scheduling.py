"""第一阶段：真实线程/SQLite/HTTP与脱敏状态桥；所有模型传输均为替身。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from pathlib import Path
from threading import Event,Thread
from urllib.request import Request,urlopen
from unittest.mock import patch
from uuid import uuid4
import json
import os
import tempfile
import time
import unittest

from dsh_tutor.resources import ResourceGate,ResourceManagedTutor,AdmissionError,load_resource_policy
from dsh_tutor.scheduler_status import SchedulerStatusPublisher,SchedulerStatusReader
from dsh_tutor.management import ManagementView
from dsh_tutor.management_http import create_management_server
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from sandbox.tests.test_storage import scope
from sandbox.tests.test_chat_session import raw_answer,data
from sandbox.tests.test_resources import wait_for


class SchedulerTests(unittest.TestCase):
    def test_round_robin_and_fifo_within_each_student(self):
        gate=ResourceGate({'max_per_student':4,'max_waiting':8});order=[]
        def work(student,name):
            with gate.slot(student):order.append(name)
        with ThreadPoolExecutor(max_workers=5) as pool:
            with gate.slot('blocker'):
                futures=[]
                for student,name in [('A','A1'),('A','A2'),('B','B1'),('B','B2'),('C','C1')]:
                    futures.append(pool.submit(work,student,name))
                    wait_for(lambda:gate.snapshot()['waiting']==len(futures))
            for f in futures:f.result(2)
        self.assertEqual(order,['A1','B1','C1','A2','B2'])

    def test_downscale_drains_running_and_does_not_admit_until_below_new_limit(self):
        gate=ResourceGate({'max_active':3,'validated_max_active':3});releases=[Event() for _ in range(3)];entered=Event()
        def active(i):
            with gate.slot(str(i)):self.assertTrue(releases[i].wait(3))
        def waiting():
            with gate.slot('waiter'):entered.set()
        with ThreadPoolExecutor(max_workers=4) as pool:
            fs=[pool.submit(active,i) for i in range(3)]
            try:
                wait_for(lambda:gate.snapshot()['active']==3)
                gate.reconfigure({'max_active':1},expected_revision=1)
                f=pool.submit(waiting);wait_for(lambda:gate.snapshot()['waiting']==1)
                for i in range(2):
                    releases[i].set();fs[i].result(2);self.assertFalse(entered.is_set())
                releases[2].set();fs[2].result(2);f.result(2);self.assertTrue(entered.is_set())
            finally:
                for r in releases:r.set()

    def test_increase_wakes_multiple_waiters_while_first_still_running(self):
        gate=ResourceGate({'max_active':1,'validated_max_active':3});release=Event()
        def work(name):
            with gate.slot(name):self.assertTrue(release.wait(3))
        with ThreadPoolExecutor(max_workers=3) as pool:
            fs=[pool.submit(work,str(i)) for i in range(3)]
            try:
                wait_for(lambda:gate.snapshot()['waiting']==2)
                gate.reconfigure({'max_active':3},expected_revision=1)
                wait_for(lambda:gate.snapshot()['active']==3)
                self.assertEqual(gate.snapshot()['waiting'],0)
            finally:release.set()
            for f in fs:f.result(2)

    def test_invalid_or_stale_update_is_atomic(self):
        gate=ResourceGate({});before=gate.snapshot()['policy']
        for changes,revision in [({'max_active':2},1),({'max_active':True},1),({'shell':True},1),({'max_waiting':2},0),({},1)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):gate.reconfigure(changes,expected_revision=revision)
            self.assertEqual(gate.snapshot()['policy'],before);self.assertEqual(gate.snapshot()['policy_revision'],1)

    def test_lower_queue_limit_keeps_existing_waiter_but_rejects_new(self):
        gate=ResourceGate({})
        def queued():
            with gate.slot('existing'):return 'ran'
        with ThreadPoolExecutor(max_workers=1) as pool:
            with gate.slot('first'):
                f=pool.submit(queued);wait_for(lambda:gate.snapshot()['waiting']==1)
                gate.reconfigure({'max_waiting':0},expected_revision=1)
                with self.assertRaises(AdmissionError) as e:
                    with gate.slot('new'):self.fail()
                self.assertEqual(e.exception.code,'queue_full');self.assertEqual(gate.snapshot()['waiting'],1)
            self.assertEqual(f.result(2),'ran')

    def test_cancelled_head_removal_allows_next_student(self):
        gate=ResourceGate({});cancel=Event();order=[]
        def work(name,event=None):
            try:
                with gate.slot(name,cancel=event):order.append(name);return 'ran'
            except AdmissionError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            with gate.slot('first'):
                a=pool.submit(work,'A',cancel);wait_for(lambda:gate.snapshot()['waiting']==1)
                b=pool.submit(work,'B');wait_for(lambda:gate.snapshot()['waiting']==2)
                cancel.set();self.assertEqual(a.result(1),'cancelled')
            self.assertEqual(b.result(1),'ran')
        self.assertEqual(order,['B']);self.assertEqual(gate.snapshot()['tracked_students'],0)

    def test_timeout_uses_original_deadline_after_reconfiguration(self):
        gate=ResourceGate({'queue_timeout_seconds':.15})
        def work():
            try:
                with gate.slot('waiter'):return 'ran'
            except AdmissionError as e:return e.code
        with ThreadPoolExecutor(max_workers=1) as pool:
            with gate.slot('first'):
                f=pool.submit(work);wait_for(lambda:gate.snapshot()['waiting']==1)
                gate.reconfigure({'queue_timeout_seconds':30.0},expected_revision=1)
                self.assertEqual(f.result(1),'queue_timeout')

    def test_live_metadata_does_not_expose_students_or_arbitrary_fields(self):
        gate=ResourceGate({});run=uuid4().hex;cancel=Event()
        with gate.slot('PRIVATE_STUDENT',run_ref=run,request_ref='a'*24,cancel=cancel):
            gate.mark(run,'model');cancel.set();s=gate.snapshot()
            self.assertEqual(s['running'][0]['stage'],'model');self.assertTrue(s['running'][0]['cancel_requested'])
            self.assertNotIn('PRIVATE_STUDENT',json.dumps(s))
        self.assertEqual(gate.snapshot()['running'],[])

    def test_rejections_have_bounded_correlated_reason_without_identity(self):
        gate=ResourceGate({'max_waiting':0})
        with gate.slot('first'):
            for i in range(40):
                with self.assertRaises(AdmissionError):
                    with gate.slot('PRIVATE_STUDENT',request_ref='a'*24):self.fail()
            recent=gate.snapshot()['recent_results']
            self.assertEqual(len(recent),32)
            self.assertTrue(all(x['error_code']=='queue_full' and x['request_ref']=='a'*24 for x in recent))
            self.assertNotIn('PRIVATE_STUDENT',json.dumps(recent))


class ApplicationSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.store=SQLiteChatStore.create(self.root/'chat.db');self.h=self.store.activate(scope())
        self.service=ResourceManagedTutor(policy={'min_free_bytes':0,'max_active':2,'validated_max_active':2},config_path='unused',state_dir=self.root)
    def tearDown(self):self.temp.cleanup()
    def req(self,rid='same'):
        return CourseQuestion(request_id=rid,question='问题',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))

    def test_parallel_students_same_request_id_have_isolated_history_and_single_replay(self):
        other=self.store.activate(scope(student_id='other',learning_context_id='other'))
        for h,secret in [(self.h,'ONLY_A'),(other,'ONLY_B')]:
            self.store.begin(h,'seed','问题');self.store.finish(h,'seed',answer=secret,run_id='old')
        def run(prompt,**kw):
            history=data(prompt)['conversation']['history'];text=json.dumps(history)
            self.assertNotEqual('ONLY_A' in text,'ONLY_B' in text)
            return raw_answer(prompt,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=run) as model:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fs=[pool.submit(self.service.answer,self.req(),store=self.store,handle=h) for h in (self.h,other)]
                self.assertEqual([f.result(3).chat.state for f in fs],['saved','saved'])
            self.assertEqual(self.service.answer(self.req(),store=self.store,handle=self.h).chat.state,'replayed')
            self.assertEqual(model.call_count,2)

    def test_inflight_duplicate_rejected_without_second_model(self):
        entered=Event();release=Event()
        def model(prompt,**kw):entered.set();self.assertTrue(release.wait(3));return raw_answer(prompt,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model) as run:
            with ThreadPoolExecutor(max_workers=1) as pool:
                f=pool.submit(self.service.answer,self.req(),store=self.store,handle=self.h)
                try:
                    self.assertTrue(entered.wait(2))
                    duplicate=self.service.answer(self.req(),store=self.store,handle=self.h)
                    self.assertEqual(duplicate.admission,'student_limit');self.assertEqual(run.call_count,1)
                finally:release.set()
                self.assertEqual(f.result(2).chat.state,'saved')

    def test_live_model_and_save_stages_precede_successful_delivery(self):
        model_entered=Event();release_model=Event();save_entered=Event();release_save=Event();finish=self.store.finish
        def model(prompt,**kw):model_entered.set();self.assertTrue(release_model.wait(3));return raw_answer(prompt,**kw)
        def save(*a,**kw):save_entered.set();self.assertTrue(release_save.wait(3));return finish(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model),patch.object(self.store,'finish',side_effect=save):
            with ThreadPoolExecutor(max_workers=1) as pool:
                f=pool.submit(self.service.answer,self.req(),store=self.store,handle=self.h)
                try:
                    self.assertTrue(model_entered.wait(2));self.assertEqual(self.service.gate.snapshot()['running'][0]['stage'],'model')
                    release_model.set();self.assertTrue(save_entered.wait(2));self.assertFalse(f.done())
                    self.assertEqual(self.service.gate.snapshot()['active'],0)
                    self.assertEqual(self.service.gate.snapshot()['finishing_requests'][0]['stage'],'save')
                finally:release_model.set();release_save.set()
                self.assertEqual(f.result(2).chat.state,'saved')

    def test_progress_observer_failure_does_not_repeat_or_invalidate_saved_answer(self):
        def broken(stage):raise RuntimeError('display unavailable')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            r=answer_chat(self.req(),store=self.store,handle=self.h,config_path='unused',state_dir=self.root,progress=broken)
        self.assertEqual(r.state,'saved');self.assertEqual(r.diagnostic_issue,'progress_unavailable');self.assertEqual(run.call_count,1)

    def test_unknown_interrupt_pauses_before_another_waiter_can_run(self):
        self.service.gate.reconfigure({'max_active':1},expected_revision=1)
        other=self.store.activate(scope(student_id='other',learning_context_id='other'))
        entered=Event();release=Event()
        def interrupted(*a,**kw):entered.set();self.assertTrue(release.wait(3));raise KeyboardInterrupt()
        with patch('dsh_tutor.runtime.run_prompt',side_effect=interrupted) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f=pool.submit(self.service.answer,self.req(),store=self.store,handle=self.h)
                try:
                    self.assertTrue(entered.wait(2));q=pool.submit(self.service.answer,self.req(),store=self.store,handle=other)
                    wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                finally:release.set()
                with self.assertRaises(KeyboardInterrupt):f.result(2)
                self.assertEqual(q.result(2).admission,'paused');self.assertEqual(run.call_count,1)

    def test_configuration_reload_only_changes_declared_live_limits(self):
        p=self.root/'policy.json';v=self.service.gate.policy.model_dump();v['max_active']=1
        p.write_text(json.dumps({'policy':v}))
        self.assertEqual(self.service.reload_limits(p,expected_revision=1),2)
        self.assertEqual(load_resource_policy(p).max_active,1)
        v['validated_max_active']=3;p.write_text(json.dumps({'policy':v}))
        with self.assertRaisesRegex(ValueError,'restart_required'):self.service.reload_limits(p,expected_revision=2)
        self.assertEqual(self.service.gate.snapshot()['policy_revision'],2)


class StatusBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.path=self.root/'status.json'
        self.gate=ResourceGate({});self.reader=SchedulerStatusReader(self.path)
    def tearDown(self):self.tmp.cleanup()

    def test_actual_publisher_reader_http_chain_and_no_plaintext(self):
        view=ManagementView(self.root/'missing.db',self.root/'unused',scheduler=self.reader)
        with SchedulerStatusPublisher(self.gate,self.path,source='simulation:scheduling-acceptance',interval=.05):
            with self.gate.slot('PRIVATE_STUDENT'):
                wait_for(lambda:self.reader().get('active')==1)
                server=create_management_server(view,'a'*40,0);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    url=f'http://127.0.0.1:{server.server_port}/v1/management/overview'
                    with urlopen(Request(url,headers={'Authorization':'Bearer '+'a'*40}),timeout=2) as r:s=json.load(r)
                    self.assertEqual(s['queue']['active'],1);self.assertEqual(s['queue']['source'],'simulation:scheduling-acceptance')
                    self.assertEqual(s['model']['state'],'unknown');self.assertNotIn('PRIVATE_STUDENT',json.dumps(s))
                finally:server.shutdown();server.server_close();thread.join(2)
        self.assertEqual(self.reader()['state'],'unknown')

    def test_missing_stale_future_and_stopped_cannot_look_idle(self):
        self.assertEqual(self.reader()['state'],'not_connected')
        with SchedulerStatusPublisher(self.gate,self.path,interval=2):
            payload=json.loads(self.path.read_text())
            for seconds in (-30,30):
                modified=payload|{'collected_at':(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()}
                self.path.write_text(json.dumps(modified));self.assertEqual(self.reader()['state'],'unknown')
        self.assertNotIn('active',self.reader())

    def test_second_publisher_cannot_overwrite_owner(self):
        with SchedulerStatusPublisher(self.gate,self.path,interval=2):
            with self.assertRaises(BlockingIOError):SchedulerStatusPublisher(ResourceGate({}),self.path).start()
            self.assertEqual(self.reader()['instance_id'],self.gate.instance_id)

    def test_invalid_large_and_linked_status_rejected(self):
        for content in ('{','x'*65537):
            self.path.write_text(content);self.assertEqual(self.reader()['state'],'unknown')
        self.path.unlink();self.path.symlink_to(self.root/'target')
        self.assertEqual(self.reader()['state'],'unknown')

    def test_fifo_cannot_block_reader(self):
        os.mkfifo(self.path);started=time.monotonic()
        self.assertEqual(self.reader()['state'],'unknown');self.assertLess(time.monotonic()-started,.2)

    def test_live_gate_not_hidden_by_database_cache(self):
        view=ManagementView(self.root/'missing.db',self.root/'unused',gate=self.gate.snapshot,ttl=60)
        self.assertEqual(view.overview()['queue']['active'],0)
        with self.gate.slot('one'):self.assertEqual(view.overview()['queue']['active'],1)
        self.assertEqual(view.overview()['queue']['active'],0)


if __name__=='__main__':unittest.main()
