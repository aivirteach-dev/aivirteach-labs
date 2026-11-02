"""管理契约和真实本地HTTP通路；虚构数据库，禁止外部模型和平台。"""
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.task_memory_api import TaskMemoryAPI
from sandbox.task_memory_demo import create_server
from sandbox.tests.test_storage import scope


class TaskMemoryAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=SQLiteChatStore.create(Path(self.temp.name)/'demo.db')
        self.h=self.store.activate(scope());self.store.migrate_learning_memory();migrate_task_memory(self.store)
        self.repo=TaskRepository(self.store);self.repo.create(self.h,'A','任务A');self.repo.create(self.h,'B','任务B')
        self.api=TaskMemoryAPI(self.repo)

    def tearDown(self):self.temp.cleanup()

    def call(self,**payload):return self.api.execute(self.h,payload)

    def test_management_roundtrip_and_shared_preference_zero_model(self):
        with patch('dsh_tutor.runtime.run_prompt') as run:
            self.call(action='edit',task_id='A',kind='goal',text='创建页面',expected_revision=0,request_id='one')
            self.call(action='edit',task_id='A',kind='preference',text='中文',expected_revision=0,request_id='two')
            a=self.call(action='view',task_id='A');b=self.call(action='view',task_id='B')
            self.assertEqual(len(a['memory']),2);self.assertEqual(len(b['memory']),1)
            self.call(action='revoke',task_id='A',kind='goal',expected_revision=1)
            self.assertNotIn('创建页面',self.call(action='view',task_id='A')['brief'])
        run.assert_not_called()

    def test_client_identity_and_unknown_or_wrong_typed_fields_rejected(self):
        for payload in [dict(action='view',task_id='A',student_id='other'),dict(action='edit',task_id='A',kind='goal',text='目标',expected_revision=True,request_id='x'),dict(action='shell'),[],None]:
            with self.assertRaises(StorageError):self.api.execute(self.h,payload)

    def test_foreign_handle_cannot_read_manage_or_list(self):
        foreign=self.h.model_copy(update={'scope':scope(student_id='other')})
        for payload in [dict(action='view',task_id='A'),dict(action='list_tasks'),dict(action='unassigned')]:
            with self.assertRaises(StorageError):self.api.execute(foreign,payload)

    def test_brief_excludes_needs_review_and_stale_edit_conflicts(self):
        for kind,text in [('goal','旧目标'),('attempt','刷新')]:self.call(action='edit',task_id='A',kind=kind,text=text,expected_revision=0,request_id=kind)
        r=self.call(action='edit',task_id='A',kind='goal',text='新目标',expected_revision=1,request_id='change')
        self.assertNotIn('刷新',r['brief']);self.assertEqual(next(x['state'] for x in r['memory'] if x['kind']=='attempt'),'needs_review')
        with self.assertRaises(StorageError):self.call(action='revoke',task_id='A',kind='goal',expected_revision=1)

    def test_http_real_roundtrip_host_origin_token_and_payload_guards(self):
        server=create_server(self.store,self.h,port=0);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        port=server.server_port
        def request(method,path,body=None,headers=None):
            connection=HTTPConnection('127.0.0.1',port,timeout=5)
            try:
                connection.request(method,path,body=body,headers=headers or {})
                response=connection.getresponse();return response.status,dict(response.getheaders()),response.read().decode()
            finally:connection.close()
        try:
            status,headers,page=request('GET','/')
            self.assertEqual(status,200);self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
            token=json.loads(re.search(r'const token=("[^"]+")',page).group(1))
            self.assertNotIn('__TOKEN_JSON__',page);self.assertNotIn('__NONCE__',page)
            payload=json.dumps(dict(action='view',task_id='A'))
            auth={'Content-Type':'application/json','X-Demo-Token':token,'Origin':f'http://127.0.0.1:{port}'}
            self.assertEqual(request('POST','/api',payload,auth)[0],200)
            self.assertEqual(request('POST','/api',payload,{'Content-Type':'application/json'})[0],403)
            self.assertEqual(request('POST','/api',payload,auth|{'Origin':'https://other.example'})[0],403)
            self.assertEqual(request('GET','/',headers={'Host':'other.example'})[0],403)
            self.assertEqual(request('GET','/api')[0],404)
            self.assertEqual(request('POST','/api','x'*8193,auth)[0],400)
            self.assertEqual(request('POST','/api','{',auth)[0],400)
            self.assertEqual(request('POST','/api',payload,auth|{'Content-Type':'text/plain'})[0],415)
            edit=dict(action='edit',task_id='A',kind='goal',text='<script>不可执行</script>',expected_revision=0,request_id='http-edit')
            self.assertEqual(request('POST','/api',json.dumps(edit),auth)[0],200)
            self.assertEqual(request('POST','/api',json.dumps(edit|{'request_id':'stale'}),auth)[0],409)
            status,_,body=request('POST','/api',json.dumps(dict(action='view',task_id='A')),auth)
            self.assertEqual(status,200);self.assertEqual(json.loads(body)['memory'][0]['text'],edit['text'])
            self.assertNotIn('innerHTML',page);self.assertIn('textContent',page)
        finally:
            server.shutdown();server.server_close();thread.join(5)

    def test_model_acceptance_rejects_arbitrary_database_path_before_io(self):
        from sandbox.task_memory_acceptance import child
        path=Path(self.temp.name)/'outside.db'
        with patch('dsh_tutor.runtime.run_prompt') as run:
            with self.assertRaises(ValueError):child('task_a',path)
        self.assertFalse(path.exists());run.assert_not_called()


if __name__=='__main__':unittest.main()
