"""阶段2：版本/授权/保存/删改/缓存/低成本路径回归，均不访问Mac。"""
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json,tempfile,time,unittest
from dsh_tutor.materials import MaterialRepository,import_bilingual
from dsh_tutor.material_publication import publish_materials,LessonCache,PREPARER
from dsh_tutor.direct_actions import DirectAction,ActionRequest
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.chat_session import ChatSession,answer_chat
from dsh_tutor.storage import StorageError
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.learning_memory import MemoryLimits
from dsh_tutor.service_contracts import Submission,ServiceError
from sandbox.integration_fixture import Fixture,fake_model
from sandbox.tests.test_materials import page
from sandbox.tests.test_chat_session import data
from sandbox.mcp_acceptance import bridge_for

class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.a=self.base/'en.html';self.z=self.base/'zh.html';self.a.write_bytes(page('en'));self.z.write_bytes(page('zh-CN'))
        self.root=self.base/'materials';self.m=import_bilingual([self.a,self.z],self.root,'course');self.repo=MaterialRepository(self.root)
    def test_publish_is_idempotent_and_prepare_reuses_public_cache(self):
        first=publish_materials(self.repo,'course',self.m['version']);self.assertEqual((self.root/'course'/self.m['version']/PREPARER).stat().st_mode & 0o777,0o755);self.assertEqual(first,publish_materials(self.repo,'course',self.m['version']))
        snap=self.repo.prepare('course',self.m['version'],'en','lesson-setup','port')
        with patch.object(self.repo,'load',side_effect=AssertionError('must not read whole course')):
            again=self.repo.prepare('course',self.m['version'],'en','lesson-setup','n8n')
        self.assertGreater(self.repo.cache.snapshot()['hits'],0);self.assertEqual(snap.material.source_sha256,again.material.source_sha256)
    def test_publication_failure_has_no_visible_partial_directory(self):
        import dsh_tutor.material_publication as module
        compile=module.compile_lesson;count=0
        def fail(doc,lesson):
            nonlocal count
            count+=1
            if count==2:raise ValueError('injected')
            return compile(doc,lesson)
        with patch.object(module,'compile_lesson',side_effect=fail):
            with self.assertRaises(ValueError):publish_materials(self.repo,'course',self.m['version'])
        self.assertFalse((self.root/'course'/self.m['version']/PREPARER).exists())
        self.assertFalse(list((self.root/'course'/self.m['version']).glob('.prepare-*')))
    def test_import_failure_does_not_publish_partial_language(self):
        write=Path.write_bytes
        def fail(path,raw):
            if path.name=='zh-CN.json':raise OSError('injected')
            return write(path,raw)
        with patch.object(Path,'write_bytes',fail):
            with self.assertRaises(OSError):import_bilingual([self.a,self.z],self.base/'other','course')
        self.assertFalse(list((self.base/'other/course').glob('import-*')))
    def test_published_tamper_is_detected_even_after_cache_hit(self):
        publish_materials(self.repo,'course',self.m['version']);self.repo.lesson('course',self.m['version'],'en','lesson-setup')
        p=self.root/'course'/self.m['version']/PREPARER/'en/lesson-setup.json';p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaises(ValueError):self.repo.lesson('course',self.m['version'],'en','lesson-setup')
    def test_cache_is_lru_bounded_and_callers_cannot_mutate_shared_content(self):
        c=LessonCache({'max_entries':2,'max_bytes':100});c.put('a',{'x':'a'});c.put('b',{'x':'b'});c.get('a')['x']='private';c.put('c',{'x':'c'})
        self.assertIsNone(c.get('b'));self.assertEqual(c.get('a'),{'x':'a'});self.assertLessEqual(c.snapshot()['bytes'],100)
        c.put('huge',{'x':'z'*200});self.assertIsNone(c.get('huge'))
    def test_zero_cache_and_version_keys_do_not_return_old_commands(self):
        old=self.repo.block('course',self.m['version'],'en','lesson-setup','b002');self.a.write_bytes(page('en',code='port: 9999'));self.z.write_bytes(page('zh-CN',code='port: 9999'))
        new=import_bilingual([self.a,self.z],self.root,'course');publish_materials(self.repo,'course',new['version'])
        self.assertNotEqual(old['text'],self.repo.block('course',new['version'],'en','lesson-setup','b002')['text'])
        no=MaterialRepository(self.root,cache_limits={'max_entries':0,'max_bytes':0});no.lesson('course',new['version'],'en','lesson-setup');self.assertEqual(no.cache.snapshot()['entries'],0)
    def test_only_explicit_goal_sections_become_course_goals(self):
        self.a.write_bytes(page('en',extra='<h2>Learning goals</h2><p>Understand scheduled workflows.</p><h2>Next</h2><p>Other.</p>'))
        self.z.write_bytes(page('zh-CN',extra='<h2>学习目标</h2><p>理解定时工作流。</p><h2>其他</h2><p>其他。</p>'))
        m=import_bilingual([self.a,self.z],self.root,'course');snap=self.repo.prepare('course',m['version'],'en','lesson-setup','goal')
        self.assertEqual(snap.goals,['Understand scheduled workflows.'])
    def test_full_code_block_action_refuses_oversize_without_truncation(self):
        self.a.write_bytes(page('en',code='x'*7000));self.z.write_bytes(page('zh-CN',code='x'*7000));m=import_bilingual([self.a,self.z],self.root,'course')
        with self.assertRaises(ValueError):self.repo.block('course',m['version'],'en','lesson-setup','b002')
        snap=self.repo.prepare('course',m['version'],'en','lesson-setup','x');self.assertEqual(snap.material.omitted_code_blocks,1)
        self.assertFalse(any('x'*100 in x for x in snap.instructions))

    def test_publication_symlink_cannot_escape_material_root(self):
        outside=self.base/'outside';outside.mkdir();(self.root/'course'/self.m['version']/PREPARER).symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):publish_materials(self.repo,'course',self.m['version'])
        self.assertEqual(list(outside.iterdir()),[])

class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.f=Fixture(self.root/'fixture');self.addCleanup(self.f.close)
        self.f.store.migrate_learning_memory();migrate_task_memory(self.f.store);repo=TaskRepository(self.f.store)
        for name,b in self.f.bindings.items():
            repo.create(b.handle,'task-a','Test');self.f.bindings[name]=replace(b,store=repo.bind(b.handle,'task-a'))
        self.f.service.verify_ready(upstream_idle=True);self.b=self.f.bindings['A']
    def action(self,rid='action-1',kind='learning_goal',**extra):
        return {'api_version':'dsh-tutor.v1','profile':'course_actions_v1','history_selection':{'mode':'none'},'payload':{'request_id':rid,'course':self.b.course.model_dump(),'action':{'kind':kind,**extra}}}
    def finish(self,raw=None,binding=None):
        v,_=self.f.service.submit(binding or self.b,raw or self.action());end=time.monotonic()+5
        while time.monotonic()<end:
            v=self.f.service.view(binding or self.b,v['operation_id'])
            if v['state'] in ('succeeded','failed','recovery_required'):return v
            time.sleep(.01)
        self.fail('operation did not finish')
    def remember(self,text='MY_GOAL',rid='goal-seed',binding=None):
        b=binding or self.b;b.store.begin(b.handle,rid,'学习目标：'+text);b.store.remember_question(b.handle,rid,'学习目标：'+text,limits=MemoryLimits());b.store.finish(b.handle,rid,answer='OK',answer_status='completed',run_id='seed-run')
    def test_goal_action_zero_model_zero_history_and_durable_replay(self):
        self.remember()
        with patch('dsh_tutor.runtime.run_prompt',side_effect=AssertionError('model forbidden')),patch.object(type(self.b.store),'read_page',side_effect=AssertionError('history forbidden')):
            v=self.finish();again=self.finish()
        self.assertEqual(v['state'],'succeeded');self.assertIn('MY_GOAL',v['result']['answer']);self.assertEqual(v['operation_id'],again['operation_id'])
        self.assertEqual(v['result']['detail']['execution_path'],'direct');self.assertEqual(self.f.service.metrics()['direct_saved'],1)
    def test_goal_change_invalidates_old_operation_and_new_action_reads_current(self):
        self.remember();v=self.finish();self.remember('NEW_GOAL','new-goal')
        old=self.f.service.view(self.b,v['operation_id']);self.assertEqual(old['state'],'failed');self.assertIsNone(old['result'])
        self.assertIn('NEW_GOAL',self.finish(self.action('after-change'))['result']['answer'])
    def test_goal_delete_never_returns_revoked_memory(self):
        self.remember();old=self.finish();item=self.b.store.learning_items(self.b.handle)[0];self.b.store.forget_learning(self.b.handle,'goal',expected_revision=item.revision)
        self.assertIsNone(self.f.service.view(self.b,old['operation_id'])['result']);self.assertNotIn('MY_GOAL',self.finish(self.action('after-delete'))['result']['answer'])
    def test_other_student_and_task_do_not_share_goals(self):
        self.remember();other=self.f.bindings['B'];raw=self.action('student-b');raw['payload']['course']=other.course.model_dump()
        self.assertNotIn('MY_GOAL',self.finish(raw,other)['result']['answer'])
        repo=TaskRepository(self.f.store);repo.create(self.b.handle,'task-b','Other');other=replace(self.b,store=repo.bind(self.b.handle,'task-b'))
        self.assertNotIn('MY_GOAL',self.finish(self.action('task-b'),other)['result']['answer'])
    def test_unknown_action_fields_and_missing_opt_in_rejected_before_dispatch(self):
        for change in ({'kind':'shell','command':'ls'},{'kind':'learning_goal','service':'n8n'},{'kind':'material_block','block_id':'../../x'}):
            raw=self.action();raw['payload']['action']=change
            with self.assertRaises(ServiceError):self.f.service.submit(self.b,raw)
        raw=self.action();raw.pop('history_selection')
        with self.assertRaises(ServiceError):self.f.service.submit(self.b,raw)
    def test_changed_action_same_request_id_conflicts(self):
        self.finish();raw=self.action(kind='service_status',service='n8n')
        with self.assertRaises(ServiceError):self.f.service.submit(self.b,raw)
    def test_fixed_action_does_not_wait_for_dsh_model_slot(self):
        held=Event();release=Event()
        def transport(*args,**kwargs):held.set();release.wait(4);return fake_model(*args,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):
            normal=self.f.request('slow-model');self.f.service.submit(self.f.bindings['B'],normal);self.assertTrue(held.wait(2))
            try:self.assertEqual(self.finish()['state'],'succeeded')
            finally:release.set()
    def service_binding(self,rid='service',allowed=True,executor=None):
        h=self.b.handle.scope;bridge=bridge_for(h.course_id,'n8n' if allowed else 'other','running',request_id=rid,instance_id=h.instance_id)
        bridge['context']['policy'].update(student_id=h.student_id,learning_context_id=h.learning_context_id,lesson_id=h.lesson_id,instance_generation=h.instance_generation)
        async def observed(service,policy):return {'state':'running','source':'simulation','note':'fixture'}
        return replace(self.b,tool_bridge=bridge,service_executor=executor or observed)
    def test_service_action_uses_existing_gateway_and_fact_validation(self):
        b=self.service_binding()
        with patch('dsh_tutor.runtime.run_prompt',side_effect=AssertionError('model forbidden')):
            v=self.finish(self.action('service','service_status',service='n8n'),b)
        self.assertEqual(v['state'],'succeeded');d=v['result']['detail'];self.assertEqual(d['quality']['facts'],'verified');self.assertEqual(d['tool_calls'],1);self.assertEqual(d['evidence'][0]['source'],'simulation')
    def test_denied_service_zero_backend_execution(self):
        calls=[]
        async def observed(service,policy):calls.append(service);return {'state':'running','source':'simulation','note':'bad'}
        b=self.service_binding(allowed=False,executor=observed);v=self.finish(self.action('service','service_status',service='n8n'),b)
        self.assertEqual(calls,[]);self.assertEqual(v['result']['detail']['tool_calls'],0);self.assertEqual(v['result']['detail']['quality']['facts'],'unavailable')
    def test_wrong_tool_scope_rejected_before_executor(self):
        b=self.service_binding();b.tool_bridge['context']['policy']['student_id']='other-student'
        v=self.finish(self.action('service','service_status',service='n8n'),b);self.assertEqual(v['state'],'failed')
    def test_backend_failure_cannot_become_evidence_or_restart_model(self):
        async def broken(*args):raise RuntimeError('private secret')
        v=self.finish(self.action('service','service_status',service='n8n'),self.service_binding(executor=broken))
        self.assertEqual(v['result']['detail']['evidence'],[]);self.assertNotIn('private secret',json.dumps(v))
    def test_no_history_mode_never_reads_or_writes_learning_memory(self):
        self.remember();captured=[]
        def transport(prompt,**kw):captured.append(data(prompt));return fake_model(prompt,**kw)
        req=CourseQuestion(request_id='course-only',course=self.b.course,question='请接着上面说')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport),patch.object(type(self.b.store),'read_page',side_effect=AssertionError('history')),patch.object(type(self.b.store),'remember_question',side_effect=AssertionError('write memory')),patch.object(type(self.b.store),'learning_items',side_effect=AssertionError('read memory')):
            r=answer_chat(req,store=self.b.store,handle=self.b.handle,config_path='unused',state_dir=self.root,history_mode='none')
        self.assertEqual(r.state,'saved');self.assertEqual(captured[0]['conversation']['history_loading'],'course_only');self.assertNotIn('MY_GOAL',json.dumps(captured[0]))
    def test_no_history_read_failure_does_not_silently_fall_back(self):
        with patch.object(type(self.b.store),'read_page',side_effect=StorageError('unavailable')),patch('dsh_tutor.runtime.run_prompt') as model:
            r=answer_chat(CourseQuestion(request_id='normal',course=self.b.course,question='continue'),store=self.b.store,handle=self.b.handle,config_path='unused',state_dir=self.root)
        self.assertEqual(r.state,'failed');model.assert_not_called()
    def test_goal_revision_change_during_direct_action_withholds_answer(self):
        self.remember();raw=self.action();a=ActionRequest.model_validate(raw['payload']);session=ChatSession(a.question(),store=self.b.store,handle=self.b.handle,config_path='unused',state_dir=self.root,history_mode='none',direct_action=a.action)
        self.assertIsNone(session.prepare());self.b.store.edit_learning(self.b.handle,'goal','CHANGED',expected_revision=1,request_id='changed');r=session.execute();self.assertEqual(r.state,'scope_changed');self.assertIsNone(r.answer)
    def test_save_failure_never_delivers_direct_answer(self):
        with patch.object(type(self.b.store),'finish',side_effect=StorageError('unavailable')):
            v=self.finish()
        self.assertEqual(v['state'],'recovery_required');self.assertIsNone(v['result'])

    def test_material_actions_are_complete_across_courses_locales_and_os(self):
        root=self.root/'materials';repository=MaterialRepository(root,cache_limits={'max_entries':2,'max_bytes':65536})
        for index,(cid,locale,osname) in enumerate([('course-n8n','zh-CN','linux'),('course-ai','en','windows'),('course-ai','zh-CN','unknown')]):
            en=self.root/'en.html';zh=self.root/'zh.html';code='echo exactly_this\n  keep_indent'
            en.write_bytes(page('en',code=code));zh.write_bytes(page('zh-CN',code=code));m=import_bilingual([en,zh],root,cid);publish_materials(repository,cid,m['version'])
            current=self.f.store.current(student_id=self.b.handle.scope.student_id,learning_context_id=self.b.handle.scope.learning_context_id)
            h=self.f.store.activate(self.b.handle.scope.model_copy(update={'course_id':cid,'course_version':m['version'],'lesson_id':'lesson-setup','environment_os':osname}),expected_revision=current.revision)
            repo=TaskRepository(self.f.store);task='material-'+str(index);repo.create(h,task,'Material');course=repository.prepare(cid,m['version'],locale,'lesson-setup','port')
            b=replace(self.b,handle=h,store=repo.bind(h,task),course=course,material_repository=repository)
            raw=self.action('material-'+str(index),'material_block',block_id='b002');raw['payload']['course']=course.model_dump()
            with patch('dsh_tutor.runtime.run_prompt',side_effect=AssertionError('model forbidden')):v=self.finish(raw,b)
            self.assertEqual(v['state'],'succeeded');self.assertIn(code,v['result']['answer']);self.assertIn('SHA256:',v['result']['answer'])
        self.assertLessEqual(repository.cache.snapshot()['entries'],2)
    def test_inspection_storage_accepts_direct_service_evidence(self):
        from dsh_tutor.lifecycle import migrate_lifecycle
        from dsh_tutor.inspection_memory import migrate_inspections
        migrate_lifecycle(self.f.store);migrate_inspections(self.f.store)
        v=self.finish(self.action('service','service_status',service='n8n'),self.service_binding())
        self.assertEqual(v['state'],'succeeded');self.assertEqual(v['result']['detail']['quality']['facts'],'verified')

    def test_no_history_mode_still_rejects_memory_edit_during_model(self):
        self.remember()
        def transport(prompt,**kwargs):
            self.b.store.edit_learning(self.b.handle,'goal','CHANGED',expected_revision=1,request_id='during-model-edit')
            return fake_model(prompt,**kwargs)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):
            r=answer_chat(CourseQuestion(request_id='none-edit',course=self.b.course,question='What?'),store=self.b.store,handle=self.b.handle,config_path='unused',state_dir=self.root,history_mode='none')
        self.assertEqual(r.state,'scope_changed');self.assertIsNone(r.answer)
    def test_material_action_missing_repository_reports_precise_failure(self):
        v=self.finish(self.action('missing-material','material_block',block_id='b001'))
        self.assertEqual(v['state'],'failed');self.assertEqual(v['error_code'],'material_repository_unavailable')
    def test_recovery_without_direct_receipt_does_not_invent_current_goal_or_run_model(self):
        self.remember();v=self.finish()
        # 模拟交付状态不明；可信管理员对账，没有回执时保守停止，不生成旧目标新副本。
        with self.f.journal.connection(write=True) as db:
            db.execute("UPDATE operations SET state='recovery_required',result_json=NULL WHERE operation_id=?",(v['operation_id'],))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=AssertionError('model forbidden')):
            self.f.service.reconcile(self.b,v['operation_id'],upstream_idle=True)
        self.assertEqual(self.f.service.view(self.b,v['operation_id'])['state'],'failed')

    def test_reconcile_pending_direct_action_cleans_source_before_next_request(self):
        from dsh_tutor.agent_service import binding_refs,digest
        a=ActionRequest.model_validate(self.action('pending-direct')['payload']);self.b.store.begin(self.b.handle,a.request_id,a.question().question)
        owner,scope=binding_refs(self.b);op='f'*32
        self.f.journal.claim(operation_id=op,owner_ref=owner,scope_ref=scope,request_id=a.request_id,digest=digest('pending'),profile='course_actions_v1',instance=self.f.service.instance)
        self.f.service.reconcile(self.b,op,upstream_idle=True)
        self.assertEqual(self.b.store.get_request(self.b.handle,a.request_id).status,'failed')
        self.f.service.verify_ready(upstream_idle=True);self.assertEqual(self.finish(self.action('after-reconcile'))['state'],'succeeded')
