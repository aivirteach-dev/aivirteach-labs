"""管理接口回归：只使用临时虚构库，验证脱敏、有界读取与HTTP权限。"""
import unittest,tempfile,sqlite3,json,threading
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from dsh_tutor.management import ManagementView,ManagementError
from dsh_tutor.management_http import create_management_server


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.db=self.root/'test.db';self.config=self.root/'config.json'
        self.config.write_text(json.dumps({'model':'test','max_output_tokens':512,'api_key':'SECRET','ollama_base_url':'PRIVATE'}))
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA user_version=5')
            db.execute('CREATE TABLE diagnostic_events(sequence INTEGER PRIMARY KEY,request_ref TEXT,state TEXT,stage TEXT,error_code TEXT,elapsed_seconds REAL,timeline_json TEXT,created_at TEXT,private TEXT)')
            for i in range(1,206):
                db.execute('INSERT INTO diagnostic_events VALUES(?,?,?,?,?,?,?,?,?)',(i,'a'*24,'failed' if i%2 else 'saved','model','timeout' if i%2 else None,2.0,'[{"stage":"model","seconds":2.0}]','2026-10-31T00:00:00+00:00','STUDENT SECRET'))
        self.view=ManagementView(self.db,self.config)
    def tearDown(self):self.tmp.cleanup()
    def test_recent_window_pagination_and_no_plaintext(self):
        p=self.view.query({});self.assertEqual(len(p['items']),20);self.assertEqual(p['items'][0]['sequence'],205)
        q=self.view.query({'before':str(p['next_before'])});self.assertEqual(q['items'][0]['sequence'],185)
        self.assertNotIn('SECRET',json.dumps(p));self.assertEqual(self.view.overview()['sample_count'],200)
    def test_error_aggregation_filters_and_time(self):
        self.assertEqual(self.view.overview()['errors'],[{'code':'timeout','count':100}])
        p=self.view.query({'state':'failed','request_ref':'a'*24,'since':'2026-10-30T00:00:00Z'})
        self.assertTrue(all(x['state']=='failed' for x in p['items']))
        self.assertEqual(self.view.query({'until':'2026-10-30T00:00:00Z'})['items'],[])
    def test_unknown_queries_and_bad_inputs_rejected(self):
        for p in ({'limit':'100000'},{'before':'-1'},{'sql':'DROP TABLE'},{'state':"' OR 1=1"},{'since':'2026-10-31'},{'request_ref':'raw-id'}):
            with self.subTest(p=p),self.assertRaises(ManagementError):self.view.query(p)
    def test_unknown_queue_not_fake_zero(self):
        s=self.view.overview();self.assertEqual(s['queue']['state'],'not_connected');self.assertNotIn('active',s['queue'])
        self.assertEqual(s['operations'],{'state':'not_connected'})
    def test_operation_metrics_are_live_filtered_and_fail_closed(self):
        raw={'counts':{'succeeded':2,'recovery_required':1,'SECRET':99},'active_operations':1,'max_inflight':5,
            'delivery_issues':0,'ready':False,'closing':False,'token':'SECRET'}
        self.view.operations=lambda:raw
        snapshot=self.view.overview()['operations'];self.assertEqual(snapshot['counts']['recovery_required'],1)
        self.assertNotIn('SECRET',json.dumps(snapshot));self.assertFalse(snapshot['ready'])
        raw['active_operations']='bad'
        self.assertEqual(self.view.overview()['operations']['state'],'unknown')
    def test_gate_is_shared_injected_instance(self):
        v=ManagementView(self.db,self.config,gate=lambda:{'active':1,'waiting':2,'paused':False,'closed':False,'student_id':'SECRET'})
        self.assertEqual(v.overview()['queue']['waiting'],2);self.assertNotIn('SECRET',json.dumps(v.overview()))
    def test_single_cached_collection(self):
        count=[];read=self.view._database_snapshot
        def tracked():count.append(1);return read()
        self.view._database_snapshot=tracked
        for _ in range(10):self.view.overview()
        self.assertEqual(len(count),1)
    def test_database_missing_not_created_and_unknown_sample_count(self):
        p=self.root/'missing.db';v=ManagementView(p,self.config)
        self.assertEqual(v.overview()['storage']['state'],'unavailable');self.assertIsNone(v.overview()['sample_count']);self.assertFalse(p.exists())
        with self.assertRaises(ManagementError):v.query({})
    def test_configuration_whitelist(self):
        s=json.dumps(self.view.configuration());self.assertIn('test',s);self.assertNotIn('SECRET',s);self.assertNotIn('PRIVATE',s)
    def test_unknown_error_text_is_not_exposed(self):
        with sqlite3.connect(self.db) as db:db.execute("UPDATE diagnostic_events SET error_code='SECRET' WHERE sequence=205")
        p=self.view.query({});self.assertEqual(p['items'][0]['error_code'],'unclassified');self.assertNotIn('SECRET',json.dumps(p))
    def test_readonly_database_unchanged(self):
        before=self.db.read_bytes();self.view.overview();self.view.query({});self.assertEqual(self.db.read_bytes(),before)
    def test_http_auth_host_origin_mutations_and_details(self):
        token='a'*40;server=create_management_server(self.view,token,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def request(path,headers=None,method='GET'):
            return urlopen(Request(base+path,headers=headers or {},method=method),timeout=2)
        auth={'Authorization':'Bearer '+token}
        try:
            for headers in ({},{**auth,'Origin':'https://evil.test'},{**auth,'Host':'evil.test'}):
                with self.assertRaises(HTTPError) as e:request('/v1/management/overview',headers)
                self.assertIn(e.exception.code,(401,403))
            with request('/v1/management/events/205',auth) as r:self.assertEqual(json.load(r)['item']['sequence'],205)
            for path,code in [('/v1/management/events/1',404),('/v1/management/events?limit=2&limit=3',400),('/v1/management/overview?sql=x',400),('/bad',404)]:
                with self.subTest(path=path),self.assertRaises(HTTPError) as e:request(path,auth)
                self.assertEqual(e.exception.code,code)
            with self.assertRaises(HTTPError) as e:request('/v1/management/overview',auth,'POST')
            self.assertEqual(e.exception.code,405)
        finally:server.shutdown();server.server_close();thread.join(2)
