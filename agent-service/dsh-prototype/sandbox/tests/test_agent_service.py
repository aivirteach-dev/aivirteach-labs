"""真实SQLite/线程/HTTP故障边界；推理用替身，不声称真实服务器容量通过。"""
from dataclasses import replace
from pathlib import Path
from threading import Event,Thread
from unittest.mock import patch
import http.client,json,sqlite3,tempfile,time,unittest,subprocess,sys
from dsh_tutor.agent_http import create_agent_server
from dsh_tutor.operation_journal import OperationJournal
from dsh_tutor.service_contracts import ServiceError,ServiceSettings,TokenUsage
from dsh_tutor.storage import StorageError
from sandbox.integration_fixture import Fixture,fake_model
from sandbox.integration_client import PlatformClient,ClientError
from sandbox.tests.test_resources import wait_for


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.fixture=Fixture(self.tmp.name)
        self.service=self.fixture.service;self.binding=self.fixture.bindings['A']
        self.service.verify_ready(upstream_idle=True)
        self.model=patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model).start()
    def tearDown(self):
        self.fixture.close();patch.stopall();self.tmp.cleanup()
    def done(self,op):
        wait_for(lambda:self.service.metrics()['active_operations']==0)
        return self.service.view(self.binding,op)
    def submit(self,rid='example-1',binding=None):
        return self.service.submit(binding or self.binding,self.fixture.request(rid))[0]['operation_id']


class ServiceTests(ServiceFixture):
    def test_saved_answer_dual_persistence_and_unknown_usage(self):
        view=self.done(self.submit())
        self.assertEqual(view['state'],'succeeded');self.assertEqual(view['result']['persistence'],
            {'agent_chat':'saved','operation_record':'saved','platform':'unconfirmed'})
        self.assertIsNone(view['result']['usage']['output_tokens']);self.assertEqual(view['result']['usage']['state'],'unknown')
        self.assertTrue(any(e['stage']=='model' for e in view['events']['items']))
        self.assertNotIn(self.fixture.request()['payload']['question'],json.dumps(view['events'],ensure_ascii=False))
        self.assertEqual(self.model.call_count,1)

    def test_lost_ack_same_id_recovers_original_operation_without_model_retry(self):
        op=self.submit();self.done(op)
        replay,created=self.service.submit(self.binding,self.fixture.request())
        self.assertFalse(created);self.assertEqual(replay['operation_id'],op);self.assertEqual(self.model.call_count,1)
        raw=self.fixture.request(question='不同问题')
        with self.assertRaises(ServiceError) as exc:self.service.submit(self.binding,raw)
        self.assertEqual(exc.exception.code,'request_conflict')

    def test_same_id_other_student_independent_and_operation_not_disclosed(self):
        op=self.submit();self.done(op);b=self.fixture.bindings['B']
        with self.assertRaises(ServiceError) as exc:self.service.view(b,op)
        self.assertEqual((exc.exception.code,exc.exception.status),('not_found',404))
        other=self.submit(binding=b);wait_for(lambda:self.service.metrics()['active_operations']==0)
        self.assertNotEqual(other,op);self.assertEqual(self.model.call_count,2)

    def test_course_text_tamper_and_student_self_report_are_rejected_before_model(self):
        raw=self.fixture.request();raw['payload']['course']['title']='恶意替换课程'
        with self.assertRaises(ServiceError) as exc:self.service.submit(self.binding,raw)
        self.assertEqual(exc.exception.code,'course_mismatch')
        raw=self.fixture.request();raw['student_id']='B'
        with self.assertRaises(ServiceError):self.service.submit(self.binding,raw)
        self.model.assert_not_called()

    def test_switch_instance_denies_old_operation_and_new_request(self):
        op=self.submit();self.done(op)
        h=self.binding.handle
        self.fixture.store.activate(h.scope.model_copy(update={'instance_generation':'g2'}),expected_revision=h.revision)
        with self.assertRaises(ServiceError) as exc:self.service.view(self.binding,op)
        self.assertEqual(exc.exception.code,'scope_changed');self.assertEqual(self.model.call_count,1)

    def test_deleted_chat_never_resurrects_operation_result_copy(self):
        op=self.submit();self.done(op);self.fixture.store.delete_history(self.binding.handle)
        view=self.service.view(self.binding,op)
        self.assertEqual((view['state'],view['error_code'],view['result']),('failed','source_changed',None))
        row=self.fixture.journal.get(op,__import__('dsh_tutor.agent_service',fromlist=['binding_refs']).binding_refs(self.binding)[0])
        self.assertIsNone(row['result_json']);self.assertEqual(self.model.call_count,1)

    def test_unavailable_chat_does_not_deliver_cached_answer(self):
        op=self.submit();self.done(op)
        with patch.object(self.fixture.store,'get_request',side_effect=StorageError('database_unavailable')):
            with self.assertRaises(ServiceError) as exc:self.service.view(self.binding,op)
        self.assertEqual(exc.exception.code,'source_unavailable')

    def test_failed_chat_save_is_not_successful_delivery(self):
        with patch.object(self.fixture.store,'finish',side_effect=StorageError('database_unavailable')):
            view=self.done(self.submit())
        self.assertNotEqual(view['state'],'succeeded');self.assertIsNone(view['result']);self.assertEqual(self.model.call_count,1)

    def test_failed_delivery_save_requires_reconciliation_but_never_repeats_inference(self):
        with patch.object(self.fixture.journal,'finish',side_effect=ServiceError('journal_unavailable',503)):
            op=self.submit();view=self.done(op)
        self.assertEqual(view['state'],'recovery_required');self.assertIsNone(view['result'])
        with self.assertRaises(ServiceError):self.service.verify_ready(upstream_idle=True)
        self.service.reconcile(self.binding,op,upstream_idle=True)
        self.service.verify_ready(upstream_idle=True)
        view=self.service.view(self.binding,op)
        self.assertEqual(view['state'],'succeeded');self.assertTrue(view['result']['replayed'])
        self.assertIsNone(view['result']['detail']);self.assertEqual(self.model.call_count,1)

    def test_progress_observer_failure_does_not_trigger_duplicate_model_or_false_ready(self):
        with patch.object(self.fixture.journal,'progress',side_effect=ServiceError('journal_unavailable',503)):
            view=self.done(self.submit())
        self.assertIn(view['state'],('succeeded','failed'));self.assertLessEqual(self.model.call_count,1)
        self.assertFalse(self.service.metrics()['ready'])

    def test_model_disconnect_has_stable_error_and_pauses_new_work(self):
        self.model.side_effect=RuntimeError('SECRET_CONNECTION_DETAILS')
        view=self.done(self.submit())
        self.assertNotEqual(view['state'],'succeeded');self.assertIsNone(view['result'])
        self.assertNotIn('SECRET_CONNECTION_DETAILS',json.dumps(view));self.assertFalse(self.service.metrics()['ready'])
        with self.assertRaises(ServiceError):self.submit('new-id')
        self.assertEqual(self.model.call_count,1)

    def test_waiting_cancel_does_not_run_second_model_and_ends_pending(self):
        entered=Event();release=Event()
        def slow(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
        self.model.side_effect=slow
        op=self.submit()
        try:
            self.assertTrue(entered.wait(2));other=self.submit(binding=self.fixture.bindings['B'])
            wait_for(lambda:self.fixture.tutor.gate.snapshot()['waiting']==1)
            self.service.cancel(self.fixture.bindings['B'],other)
            wait_for(lambda:self.service.view(self.fixture.bindings['B'],other)['state']=='failed')
            self.assertEqual(self.service.view(self.fixture.bindings['B'],other)['error_code'],'cancelled')
            self.assertEqual(self.model.call_count,1)
        finally:release.set()
        self.done(op)

    def test_active_cancel_keeps_slot_until_actual_return_and_preserves_saved_answer(self):
        entered=Event();release=Event()
        def slow(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
        self.model.side_effect=slow;op=self.submit()
        try:
            self.assertTrue(entered.wait(2));view=self.service.cancel(self.binding,op)
            self.assertEqual(view['cancellation'],'requested_unconfirmed')
            self.assertEqual(self.fixture.tutor.gate.snapshot()['active'],1)
            self.assertEqual(self.service.metrics()['active_operations'],1)
        finally:release.set()
        view=self.done(op);self.assertEqual(view['cancellation'],'completed_after_cancel')
        self.assertEqual(self.model.call_count,1)

    def test_service_bounds_pool_and_per_student_before_submission(self):
        entered=Event();release=Event()
        def slow(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
        self.model.side_effect=slow;op=self.submit()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ServiceError) as exc:self.submit('second')
            self.assertEqual(exc.exception.code,'student_busy')
            for name in 'BCDE':self.submit(binding=self.fixture.bindings[name])
            with self.assertRaises(ServiceError) as exc:self.submit(binding=self.fixture.bindings['F'])
            self.assertEqual(exc.exception.code,'overloaded');self.assertEqual(self.service.metrics()['active_operations'],5)
            for name in 'BCDE':
                from dsh_tutor.agent_service import binding_refs
                row=self.fixture.journal.find(binding_refs(self.fixture.bindings[name])[0],'example-1')
                self.service.cancel(self.fixture.bindings[name],row['operation_id'])
        finally:release.set()
        self.done(op)

    def test_service_stop_does_not_release_owner_lock_while_model_still_running(self):
        entered=Event();release=Event()
        def slow(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
        self.model.side_effect=slow;self.submit()
        try:
            self.assertTrue(entered.wait(2));self.assertFalse(self.service.close(timeout=.01))
            with self.assertRaises(ServiceError) as exc:OperationJournal(self.fixture.root/'operations.db')
            self.assertEqual(exc.exception.code,'journal_owned')
        finally:release.set()
        wait_for(lambda:self.service.metrics()['active_operations']==0)

    def test_tool_failure_remains_partial_not_fabricated_successful_observation(self):
        from dsh_tutor.contracts import CourseAnswer
        def tool_failure(request,**kw):
            return CourseAnswer(request_id=request.request_id,run_id='mock-tool-run',status='partial',
                answer='工具连接失败，无法确认服务状态。',model='simulation-only',elapsed_seconds=.01,
                tool_attempts=1,tool_trace=[{'tool':'get_service_status','status':'error','code':'tool_unavailable'}],
                quality={'facts':'unavailable'},limitations=['模拟工具不可用，未观察到真实环境。']).model_dump()
        with patch('dsh_tutor.chat_session.answer_course',side_effect=tool_failure):view=self.done(self.submit())
        self.assertEqual(view['state'],'succeeded');self.assertEqual(view['result']['answer_status'],'partial')
        self.assertEqual(view['result']['detail']['quality']['facts'],'unavailable');self.model.assert_not_called()

    def test_task_bound_storage_still_limits_get_request_and_result_delivery(self):
        from dsh_tutor.task_memory import migrate_task_memory,TaskRepository
        self.fixture.store.migrate_learning_memory();migrate_task_memory(self.fixture.store)
        repo=TaskRepository(self.fixture.store);repo.create(self.binding.handle,'task-a','A');repo.create(self.binding.handle,'task-b','B')
        a=repo.bind(self.binding.handle,'task-a');b=repo.bind(self.binding.handle,'task-b')
        self.binding=replace(self.binding,store=a)
        op=self.submit();view=self.done(op);self.assertEqual(view['state'],'succeeded')
        self.assertIsNone(b.get_request(self.binding.handle,'example-1'))
        with self.assertRaises(ServiceError):self.service.view(replace(self.binding,store=b),op)


class DeadlineAndRestartTests(unittest.TestCase):
    def test_management_demo_migrated_database_uses_task_bound_entry(self):
        from sandbox.integration_demo import IntegrationDemo
        with tempfile.TemporaryDirectory() as root:
            demo=IntegrationDemo(root).start()
            try:
                demo.worker.join(3);self.assertFalse(demo.worker.is_alive());self.assertIsNone(demo.error_code)
                wait_for(lambda:demo.service.metrics()['active_operations']==0,timeout=5)
                counts=demo.service.metrics()['counts']
                self.assertEqual(sum(counts.values()),5);self.assertGreaterEqual(counts.get('succeeded',0),4)
                from dsh_tutor.management import ManagementView
                view=ManagementView(demo.database,Path(root)/'not-needed-config',operations=demo.service.metrics)
                result=view.overview();self.assertEqual(result['operations']['counts'],counts)
                self.assertFalse(any(x['code']=='task_binding_required' for x in result['errors']))
            finally:demo.close()

    def test_soft_deadline_is_persisted_but_does_not_release_running_model(self):
        with tempfile.TemporaryDirectory() as root:
            fixture=Fixture(root,settings={'operation_timeout_seconds':.1});entered=Event();release=Event()
            def slow(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
            try:
                with patch('dsh_tutor.runtime.run_prompt',side_effect=slow) as model:
                    fixture.service.verify_ready(upstream_idle=True)
                    op=fixture.service.submit(fixture.bindings['A'],fixture.request())[0]['operation_id']
                    try:
                        self.assertTrue(entered.wait(2))
                        wait_for(lambda:fixture.service.view(fixture.bindings['A'],op)['deadline_exceeded'])
                        self.assertEqual(fixture.tutor.gate.snapshot()['active'],1);self.assertFalse(fixture.service.metrics()['ready'])
                    finally:release.set()
                    wait_for(lambda:fixture.service.metrics()['active_operations']==0)
                    view=fixture.service.view(fixture.bindings['A'],op)
                    self.assertTrue(view['deadline_exceeded']);self.assertEqual(model.call_count,1)
                    self.assertEqual(view['cancellation'],'completed_after_cancel')
            finally:release.set();fixture.close()

    def _crash(self,mode):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root);child=subprocess.Popen([sys.executable,'-m','sandbox.integration_crash_worker',root,mode],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                marker=path/('delivery-entered' if mode=='saved' else 'model-count.txt')
                wait_for(lambda:(path/'operation.json').exists() and marker.exists(),timeout=8)
                op=json.loads((path/'operation.json').read_text())['operation_id']
            finally:
                if child.poll() is None:child.kill()
                child.communicate(timeout=3)
            fixture=Fixture(root,reopen=True)
            try:
                with patch('dsh_tutor.runtime.run_prompt') as model:
                    binding=fixture.bindings['A'];view=fixture.service.view(binding,op)
                    self.assertEqual(view['state'],'recovery_required');self.assertIsNone(view['result'])
                    self.assertFalse(fixture.service.metrics()['ready'])
                    with self.assertRaises(ServiceError):fixture.service.verify_ready(upstream_idle=True)
                    with self.assertRaises(ServiceError):fixture.service.reconcile(binding,op,upstream_idle=False)
                    fixture.service.reconcile(binding,op,upstream_idle=True)
                    fixture.service.verify_ready(upstream_idle=True)
                    view=fixture.service.view(binding,op)
                    self.assertEqual(view['state'],'succeeded' if mode=='saved' else 'failed')
                    if mode=='saved':self.assertTrue(view['result']['replayed']);self.assertIsNone(view['result']['detail'])
                    else:self.assertIsNone(view['result']);self.assertEqual(fixture.store.get_request(binding.handle,'example-1').status,'failed')
                    replay,created=fixture.service.submit(binding,fixture.request())
                    self.assertFalse(created);self.assertEqual(replay['operation_id'],op);model.assert_not_called()
                    self.assertEqual((path/'model-count.txt').read_text(),'1')
            finally:fixture.close()
    def test_killed_process_during_model_requires_explicit_pending_reconciliation(self):self._crash('pending')
    def test_killed_process_after_chat_save_recovers_saved_answer_without_new_model(self):self._crash('saved')


class HttpTests(ServiceFixture):
    def setUp(self):
        super().setUp();self.server=create_agent_server(self.service,self.fixture.resolve,port=0)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=PlatformClient(f'http://127.0.0.1:{self.server.server_port}',self.fixture.tokens['A'])
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(1);super().tearDown()
    def raw(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            base={'Authorization':'Bearer '+self.fixture.tokens['A'],'Content-Type':'application/json'};base.update(headers or {})
            conn.request(method,path,body=body,headers=base);response=conn.getresponse()
            return response.status,json.loads(response.read())
        finally:conn.close()

    def test_real_http_submit_poll_and_manual_duplicate(self):
        status,view=self.client.submit(self.fixture.request());self.assertEqual(status,202)
        op=view['operation_id'];self.done(op)
        view=self.client.view(op);self.assertEqual(view['state'],'succeeded')
        status,replay=self.client.submit(self.fixture.request());self.assertEqual(status,200)
        self.assertEqual(replay['operation_id'],op);self.assertEqual(self.model.call_count,1)
        delta=self.client.view(op,view['events']['next_after']);self.assertEqual(delta['events']['items'],[])

    def test_authentication_host_origin_and_other_student_boundaries(self):
        for headers,code in [({'Authorization':'Bearer '+'x'*40},401),({'Host':'evil.test'},403),({'Origin':'http://evil.test'},401)]:
            self.assertEqual(self.raw('GET','/v1/tutor/capabilities',headers=headers)[0],code)
        op=self.submit();self.done(op)
        client=PlatformClient(self.client.base,self.fixture.tokens['B'])
        with self.assertRaises(ClientError) as exc:client.view(op)
        self.assertEqual(exc.exception.status,404)

    def test_nonfinite_duplicate_keys_and_array_json_rejected(self):
        for raw in ['{"a":NaN}','{"a":1,"a":2}','[]','{"a":"x"']:
            self.assertEqual(self.raw('POST','/v1/tutor/operations',raw)[0],400)
        self.model.assert_not_called()

    def test_body_limit_cursor_query_and_no_admin_route(self):
        self.assertEqual(self.raw('POST','/v1/tutor/operations','x'*32769)[0],413)
        op=self.submit();self.done(op)
        for suffix in ['?after=-1','?after=0&after=1','?other=1']:
            self.assertEqual(self.raw('GET','/v1/tutor/operations/'+op+suffix)[0],400 if 'other' not in suffix else 404)
        self.assertEqual(self.raw('POST','/v1/tutor/resume','{}')[0],404)
        self.assertEqual(self.raw('POST',f'/v1/tutor/operations/{op}/cancel','{"force":true}')[0],400)

    def test_rate_limit_is_bounded_and_does_not_call_model(self):
        statuses=[self.raw('GET','/v1/tutor/capabilities')[0] for _ in range(self.fixture.service.settings.http_budget.query_per_second+5)]
        self.assertIn(429,statuses);self.model.assert_not_called()

    def test_read_rate_limit_preserves_submit_and_cancel_routes(self):
        for _ in range(self.fixture.service.settings.http_budget.query_per_second+2):
            self.raw('GET','/v1/tutor/capabilities')
        status,view=self.client.submit(self.fixture.request('control-after-query-flood'))
        self.assertEqual(status,202)
        cancelled=self.client.cancel(view['operation_id'])
        self.assertEqual(cancelled['operation_id'],view['operation_id'])

    def test_native_profile_through_http_uses_authoritative_course_and_partial_projection(self):
        from sandbox.tests.test_platform_adapter import platform_request
        from dsh_tutor.platform_adapter import adapt_platform,platform_response
        raw=platform_request();raw['course']['version']='v1'
        canonical=adapt_platform(raw,lab_id='demo-lab')
        self.fixture.bindings['A']=replace(self.binding,course=canonical.course)
        self.binding=self.fixture.bindings['A']
        status,view=self.client.submit({'api_version':'dsh-tutor.v1','profile':'aivirteach','payload':raw})
        self.assertEqual(status,202);view=self.done(view['operation_id'])
        self.assertEqual(view['state'],'succeeded');self.assertIn('原始摘录内容',self.model.call_args.args[0])
        self.assertEqual(platform_response(view)['status'],'partial');self.assertEqual(self.model.call_count,1)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ops.db'
        self.journal=OperationJournal.create(self.path,{'max_operations':1})
    def tearDown(self):self.journal.close();self.tmp.cleanup()
    def claim(self,op='a'):
        return self.journal.claim(operation_id=op,owner_ref='owner',scope_ref='scope',request_id=op,digest='digest',profile='course_snapshot',instance='first')
    def test_exclusive_owner_missing_file_and_wrong_schema_fail_closed(self):
        with self.assertRaises(ServiceError):OperationJournal(self.path)
        with self.assertRaises(ServiceError):OperationJournal(self.path.parent/'missing.db')
        self.journal.close()
        db=sqlite3.connect(self.path);db.execute('PRAGMA user_version=99');db.close()
        with self.assertRaises(ServiceError) as exc:OperationJournal(self.path)
        self.assertEqual(exc.exception.code,'unsupported_journal')
    def test_journal_capacity_has_no_automatic_deletion_or_retry(self):
        self.claim();self.journal.finish('a','first',state='failed',code='cancelled')
        with self.assertRaises(ServiceError) as exc:self.claim('b')
        self.assertEqual(exc.exception.code,'journal_capacity');self.assertEqual(self.journal.counts(),{'failed':1})
    def test_event_window_cursor_reset_and_deadline_persist_after_cancel(self):
        self.claim()
        for i in range(40):self.journal.progress('a','first','preparation_wait' if i%2 else 'model')
        events=self.journal.events('a',0);self.assertEqual(len(events['items']),24);self.assertTrue(events['reset_required'])
        self.journal.cancel('a','owner');self.journal.cancel('a','owner','operation_deadline')
        self.assertTrue(self.journal.get('a','owner')['deadline_exceeded'])
        with self.assertRaises(ServiceError):self.journal.events('a',9999)
    def test_reopen_marks_interrupted_but_does_not_start_any_model(self):
        self.claim();self.journal.close();self.journal=OperationJournal(self.path,{'max_operations':1})
        self.journal.begin_session('new')
        self.assertEqual(self.journal.get('a','owner')['state'],'recovery_required')
        with self.assertRaises(ServiceError):self.journal.finish('a','first',state='failed')
    def test_result_limit_keeps_pending_operation_without_success(self):
        self.claim()
        with self.assertRaises(ServiceError):self.journal.finish('a','first',state='succeeded',result={'answer':'x'*70000})
        self.assertEqual(self.journal.get('a','owner')['state'],'queued')
    def test_usage_unknown_cannot_be_disguised_as_zero(self):
        with self.assertRaises(ValueError):TokenUsage(output_tokens=0)
        with self.assertRaises(ValueError):TokenUsage(state='known',output_tokens=1)
        self.assertIsNone(TokenUsage().output_tokens)

    def test_locked_database_returns_bounded_sanitized_error(self):
        db=sqlite3.connect(self.path);db.execute('BEGIN EXCLUSIVE')
        try:
            started=time.monotonic()
            with self.assertRaises(ServiceError) as exc:self.claim()
            self.assertLess(time.monotonic()-started,.6);self.assertEqual(exc.exception.code,'journal_unavailable')
            self.assertNotIn(str(self.path),str(exc.exception))
        finally:db.rollback();db.close()
