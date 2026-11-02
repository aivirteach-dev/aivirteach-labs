"""资源门控与真实应用入口组合回归；不用Mac模型，不把线程任务当推理容量。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event,Lock
import tempfile
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from dsh_tutor.resources import ResourceGate,ResourcePolicy,AdmissionError,ResourceManagedTutor,assess_capacity
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.contracts import CourseQuestion
from sandbox.tests.test_storage import scope
from sandbox.tests.test_chat_session import raw_answer


def wait_for(predicate,timeout=2):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('状态未及时发生')
        time.sleep(0.005)


class GateTests(unittest.TestCase):
    def test_unknown_capacity_defaults_to_one_and_higher_needs_validation(self):
        self.assertEqual(ResourcePolicy().max_active,1)
        with self.assertRaises(ValidationError):ResourcePolicy(max_active=2)
        with self.assertRaises(ValidationError):ResourcePolicy(max_waiting=-1)

    def test_peak_active_never_exceeds_limit_and_fifo_order(self):
        gate=ResourceGate({'max_waiting':4});release=Event();order=[]
        def task(name,hold=False):
            with gate.slot(name):
                self.assertEqual(gate.snapshot()['active'],1);order.append(name)
                if hold:self.assertTrue(release.wait(3))
        with ThreadPoolExecutor(max_workers=4) as pool:
            first=pool.submit(task,'first',True);wait_for(lambda:gate.snapshot()['active']==1)
            second=pool.submit(task,'second');wait_for(lambda:gate.snapshot()['waiting']==1)
            third=pool.submit(task,'third');wait_for(lambda:gate.snapshot()['waiting']==2)
            release.set()
            for x in (first,second,third):x.result(timeout=3)
        self.assertEqual(order,['first','second','third'])
        self.assertEqual(gate.snapshot()['tracked_students'],0)

    def test_queue_full_and_student_limit_reject_without_running(self):
        gate=ResourceGate({'max_waiting':0})
        with gate.slot('student'):
            for name,code in [('other','queue_full'),('student','student_limit')]:
                with self.assertRaises(AdmissionError) as exc:
                    with gate.slot(name):self.fail('不得执行')
                self.assertEqual(exc.exception.code,code)
        self.assertEqual(gate.snapshot()['active'],0)

    def test_queue_timeout_releases_reservation(self):
        gate=ResourceGate({'queue_timeout_seconds':0.05})
        with gate.slot('first'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                def queued():
                    try:
                        with gate.slot('second'):return 'ran'
                    except AdmissionError as exc:return exc.code
                self.assertEqual(pool.submit(queued).result(timeout=1),'queue_timeout')
            self.assertEqual(gate.snapshot()['waiting'],0)
            self.assertEqual(gate.snapshot()['tracked_students'],1)

    def test_waiting_cancel_has_no_task_and_releases_student(self):
        gate=ResourceGate({});cancel=Event()
        def task():
            try:
                with gate.slot('waiter',cancel=cancel):return 'ran'
            except AdmissionError as exc:return exc.code
        with gate.slot('first'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(task);wait_for(lambda:gate.snapshot()['waiting']==1);cancel.set()
                self.assertEqual(future.result(timeout=1),'cancelled')
            self.assertEqual(gate.snapshot()['tracked_students'],1)

    def test_exception_releases_slot(self):
        gate=ResourceGate({})
        with self.assertRaises(RuntimeError):
            with gate.slot('student'):raise RuntimeError('test')
        with gate.slot('other'):pass
        self.assertEqual(gate.snapshot()['counts']['task_exception'],1)

    def test_close_rejects_waiters_but_keeps_running_slot(self):
        gate=ResourceGate({})
        def task():
            try:
                with gate.slot('waiter'):return 'ran'
            except AdmissionError as exc:return exc.code
        with gate.slot('first'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(task);wait_for(lambda:gate.snapshot()['waiting']==1);gate.close()
                self.assertEqual(future.result(timeout=1),'closed');self.assertEqual(gate.snapshot()['active'],1)
        self.assertEqual(gate.snapshot()['active'],0)

    def test_pause_requires_explicit_resume(self):
        gate=ResourceGate({});gate.pause()
        with self.assertRaises(AdmissionError):
            with gate.slot('student'):self.fail()
        gate.resume_after_verification()
        with gate.slot('student'):pass

    def test_metrics_are_bounded_and_do_not_include_student_identity(self):
        gate=ResourceGate({})
        for i in range(100):
            with gate.slot('PRIVATE_CANARY'):pass
        snap=gate.snapshot()
        self.assertEqual(snap['application_seconds']['samples'],64)
        self.assertEqual(snap['counts']['finished'],100)
        self.assertNotIn('PRIVATE_CANARY',str(snap));self.assertEqual(snap['tracked_students'],0)

    def test_capacity_assessment_never_changes_configuration(self):
        self.assertIsNone(assess_capacity(expected_online=50)['load_ratio'])
        self.assertEqual(assess_capacity(peak_requests_per_minute=8,measured_requests_per_minute=4)['load_ratio'],2)
        self.assertFalse(assess_capacity(expected_online=1000)['automatic_changes'])
        for value in [float('nan'),float('inf'),-1,True]:
            with self.assertRaises(ValueError):assess_capacity(expected_online=value)


class ManagedTutorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=SQLiteChatStore.create(Path(self.temp.name)/'chat.db')
        self.h=self.store.activate(scope())
        self.service=ResourceManagedTutor(policy={'min_free_bytes':0},config_path='unused',state_dir=self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def req(self,rid='r'):
        return CourseQuestion(request_id=rid,question='问题',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
    def test_actual_application_flow_saves_and_replays_under_gate(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer) as run:
            first=self.service.answer(self.req(),store=self.store,handle=self.h)
            second=self.service.answer(self.req(),store=self.store,handle=self.h)
        self.assertEqual(first.chat.state,'saved');self.assertEqual(second.chat.state,'replayed')
        self.assertEqual(run.call_count,1)
        self.assertEqual(self.service.gate.snapshot()['counts']['result_saved'],1)
    def test_disk_low_and_disk_error_prevent_database_accept_and_model(self):
        from dsh_tutor.resources import AdmissionError
        for code in ['disk_low','disk_unavailable']:
            with patch('dsh_tutor.resources.disk_check',side_effect=AdmissionError(code)):
                with patch('dsh_tutor.runtime.run_prompt') as run:result=self.service.answer(self.req(),store=self.store,handle=self.h)
            self.assertEqual(result.admission,code);run.assert_not_called()
        self.assertEqual(self.store.read_page(self.h).turns,[])
    def test_running_cancel_does_not_prematurely_release_slot(self):
        cancel=Event();entered=Event();release=Event()
        def run(prompt,**kwargs):entered.set();release.wait(3);return raw_answer(prompt,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=run):
            with ThreadPoolExecutor(max_workers=1) as pool:
                f=pool.submit(self.service.answer,self.req(),store=self.store,handle=self.h,cancel=cancel)
                self.assertTrue(entered.wait(2));cancel.set()
                self.assertEqual(self.service.gate.snapshot()['active'],1)
                release.set();self.assertEqual(f.result(timeout=3).chat.state,'saved')
        self.assertEqual(self.service.gate.snapshot()['active'],0)
    def test_model_failure_pauses_future_admission_until_verified(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeError('provider failed')) as run:
            first=self.service.answer(self.req(),store=self.store,handle=self.h)
            second=self.service.answer(self.req('second'),store=self.store,handle=self.h)
        self.assertEqual(first.chat.error_code,'run_failed');self.assertEqual(second.admission,'paused')
        self.assertEqual(run.call_count,1)
    def test_queued_request_scope_switch_rejected_before_model(self):
        other=self.store.activate(scope(student_id='other',learning_context_id='other'))
        # 占住唯一槽，队列中的本学生切换修订后不得运行旧请求。
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch('dsh_tutor.runtime.run_prompt') as run:
                with self.service.gate.slot(other.scope.student_id):
                    future=pool.submit(self.service.answer,self.req(),store=self.store,handle=self.h)
                    wait_for(lambda:self.service.gate.snapshot()['waiting']==1)
                    self.store.activate(scope(instance_generation='g2'),expected_revision=1)
                result=future.result(timeout=2)
                self.assertEqual(result.chat.state,'scope_changed');run.assert_not_called()

    def test_learning_memory_remains_available_under_resource_wrapper(self):
        from dsh_tutor.learning_memory import MemoryLimits
        self.store.migrate_learning_memory()
        self.store.begin(self.h,'memory','偏好：简短中文')
        self.store.remember_question(self.h,'memory','偏好：简短中文',limits=MemoryLimits())
        self.store.finish(self.h,'memory',answer='收到')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=raw_answer):
            result=self.service.answer(self.req(),store=self.store,handle=self.h,learning_memory=self.store)
        self.assertEqual(result.chat.state,'saved')
        self.assertEqual(result.chat.history.memory_items,1)


if __name__=='__main__':unittest.main()
