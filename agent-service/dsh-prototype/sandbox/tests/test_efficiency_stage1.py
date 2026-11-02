"""阶段1：无Mac/真实LLM；固定本机服务验证安装的DSH及资源边界。"""
import json,os,pwd,tempfile,threading,time,unittest,subprocess
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from dsh_tutor.runtime import ROOT,load_config,run_prompt
from dsh_tutor.execution_budget import RuntimeFailure,RuntimeMetrics,ExecutionLimits,observe_runtime,execution_projection
from dsh_tutor.http_budget import HttpBudget
from dsh_tutor.polling import ProgressPoller
from dsh_tutor.service_contracts import ServiceError
from sandbox.mcp_acceptance import bridge_for


from sandbox.efficiency_fixture import fake_provider


class ActualDshBudgetTests(unittest.TestCase):
    def run_case(self,mode='text',limits=None,bridge=False,prompt='Only reply probe.',delay=0):
        with tempfile.TemporaryDirectory() as directory,fake_provider(mode,delay) as (url,received):
            path=Path(directory);config=load_config(ROOT/'config.json')
            config.update(ollama_base_url=url,execution_user=pwd.getpwuid(os.geteuid()).pw_name,total_timeout_seconds=20)
            if limits:config['execution_limits'].update(limits)
            cfg=path/'config.json';cfg.write_text(json.dumps(config));stages=[]
            b=bridge_for('course','n8n','stopped',request_id='budget',instance_id='fake-vm') if bridge else None
            result=None;error=None
            try:
                with observe_runtime(stages.append):result=run_prompt(prompt,config_path=cfg,state_dir=path/'runs',tool_bridge=b)
            except RuntimeFailure as exc:error=exc
            audits=list((path/'runs').glob('*/tool-audit.jsonl'))
            rows=[json.loads(line) for file in audits for line in file.read_text().splitlines()]
            left=[p.name for p in (path/'runs').glob('*/*') if p.name in ('request.json','stdout.tmp','stderr.tmp','runtime.patch.yml','tool-context.json')]
            return result,error,received,stages,rows,left

    def test_actual_guard_installed_before_first_call_and_metrics_are_sanitized(self):
        result,error,received,stages,_,left=self.run_case(delay=.08)
        self.assertIsNone(error);self.assertEqual(len(received),1);self.assertEqual(result['answer'],'probe')
        metrics=result['runtime_metrics'];self.assertEqual(metrics['model_calls'],1)
        self.assertEqual(metrics['output_reserved_tokens'],512);self.assertEqual(metrics['output_bytes'],5)
        self.assertGreaterEqual(metrics['timings']['model_stream'],.07)
        self.assertIn('first_visible',metrics['timings']);self.assertIn('dsh_startup',metrics['timings'])
        self.assertGreater(metrics['timings']['dsh_startup'],.01)
        self.assertIn('worker_start',stages);self.assertIn('model_request',stages)
        self.assertEqual(metrics['actual_usage'],'unknown');self.assertEqual(left,[])
        self.assertNotIn('probe',json.dumps(metrics));self.assertFalse(received[0].get('tools'))

    def test_tool_continuation_cannot_bypass_request_call_budget(self):
        _,error,received,_,rows,left=self.run_case('tool',{'max_model_calls':1},True)
        self.assertIsNotNone(error);self.assertEqual(error.code,'model_budget_exhausted')
        self.assertEqual(len(received),1);self.assertEqual(error.metrics['budget_stop'],'model_calls')
        self.assertEqual(sum(r['type']=='execution' for r in rows),1);self.assertEqual(left,[])

    def test_output_reservation_prevents_second_model_request(self):
        _,error,received,_,_,_=self.run_case('tool',{'max_total_output_tokens':512},True)
        self.assertEqual(len(received),1);self.assertEqual(error.metrics['budget_stop'],'output_reservation')

    def test_oversized_input_rejected_before_provider(self):
        _,error,received,_,_,_=self.run_case(limits={'max_input_estimated_tokens_per_call':128},prompt='课程资料'*500)
        self.assertEqual(received,[]);self.assertEqual(error.code,'model_budget_exhausted')
        self.assertEqual(error.metrics['model_calls'],0)

    def test_provider_error_not_automatically_retried(self):
        _,error,received,_,_,_=self.run_case('failure')
        self.assertIsNotNone(error);self.assertEqual(len(received),1)

    def test_stream_output_over_budget_remains_unconfirmed(self):
        _,error,received,_,_,_=self.run_case('large',{'max_output_bytes':128})
        self.assertEqual(len(received),1);self.assertEqual(error.code,'provider_outcome_unknown')
        self.assertEqual(error.metrics['budget_stop'],'output_bytes')
        self.assertGreater(error.metrics['unconfirmed_calls'],0)

    def test_two_new_runs_have_independent_call_budgets(self):
        for _ in range(2):
            result,error,received,_,_,_=self.run_case(limits={'max_model_calls':1})
            self.assertIsNone(error);self.assertEqual(len(received),1);self.assertEqual(result['runtime_metrics']['model_calls'],1)


class HttpAndPollingBudgetTests(unittest.TestCase):
    def test_query_flood_cannot_spend_submit_or_cancel_rate_budget(self):
        now=[10.0];budget=HttpBudget({},clock=lambda:now[0])
        for _ in range(40):budget.check('query',authenticated=True)
        with self.assertRaises(ServiceError):budget.check('query',authenticated=True)
        budget.check('cancel',authenticated=True);budget.check('submit',authenticated=True)
        now[0]+=1.01;budget.check('query',authenticated=True)

    def test_authentication_timeout_keeps_query_slots_until_actual_return(self):
        budget=HttpBudget({'resolver_timeout_seconds':.01});release=threading.Event();started=[]
        def slow(token):started.append(token);release.wait(1);return token
        try:
            for i in range(4):
                with self.assertRaises(ServiceError) as exc:budget.resolve(slow,str(i),'query')
                self.assertEqual(exc.exception.code,'authentication_timeout')
            with self.assertRaises(ServiceError) as exc:budget.resolve(slow,'fifth','query')
            self.assertEqual(exc.exception.code,'authentication_busy');self.assertEqual(len(started),4)
            self.assertEqual(budget.resolve(lambda _: 'control','unused','cancel'),'control')
        finally:release.set()

    def test_pre_authentication_attempts_are_bounded_separately(self):
        budget=HttpBudget({'query_per_second':1})
        for _ in range(3):budget.check('query')
        with self.assertRaises(ServiceError):budget.check('query')
        budget.check('cancel')

    def test_poller_backs_off_resets_honors_retry_after_and_pauses(self):
        poll=ProgressPoller(random_value=lambda:.5)
        first=poll.delay(cursor=1);second=poll.delay(cursor=1)
        self.assertGreater(second,first);self.assertEqual(poll.delay(cursor=2),first)
        self.assertGreaterEqual(poll.delay(cursor=2,retry_after=4),4)
        self.assertIsNone(poll.delay(visible=False));self.assertIsNone(poll.delay(terminal=True))
        self.assertLessEqual(max(poll.delay(cursor=2) for _ in range(30)),8)

    def test_metrics_reject_sensitive_or_wrongly_typed_fields(self):
        with self.assertRaises(ValueError):RuntimeMetrics(model_calls=0,input_estimated_tokens=0,output_reserved_tokens=0,output_bytes=0,prompt='secret')
        with self.assertRaises(ValueError):RuntimeMetrics(model_calls=True,input_estimated_tokens=0,output_reserved_tokens=0,output_bytes=0)
        with self.assertRaises(ValueError):RuntimeMetrics(model_calls=0,input_estimated_tokens=0,output_reserved_tokens=0,output_bytes=0,timings={'secret':1.0}).safe()

    def test_javascript_public_plugin_paths_not_just_python_counter(self):
        node=ROOT/'.runtime/node-v22.13.0-linux-x64/bin/node'
        result=subprocess.run([str(node),'--test',str(ROOT/'sandbox/tests/test_model_budget.mjs')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class ProfileAndTraceTests(unittest.TestCase):
    def test_actual_profile_uses_guard_and_disables_extra_model_producers(self):
        from tools.check_execution_profile import check_profile
        record=check_profile()
        self.assertEqual(record['status'],'passed');self.assertEqual(record['model_calls'],0)
        self.assertIn('compaction-basic',record['disabled']);self.assertIn('llm-retry',record['disabled'])

    def test_runtime_observer_does_not_leak_to_other_threads_or_next_request(self):
        from dsh_tutor.execution_budget import runtime_stage
        local=[]
        with observe_runtime(local.append):
            runtime_stage('model_request')
            thread=threading.Thread(target=lambda:runtime_stage('tool_execution'));thread.start();thread.join()
        runtime_stage('runtime_finish');self.assertEqual(local,['model_request'])

    def test_execution_summary_is_window_bounded_and_unknown_usage_stays_unknown(self):
        row=RuntimeMetrics(model_calls=1,input_estimated_tokens=5,output_reserved_tokens=512,output_bytes=2,timings={'model_stream':.1}).safe()
        result=execution_projection([row for _ in range(100)])
        self.assertEqual(result['samples'],64);self.assertEqual(result['model_calls'],64)
        self.assertEqual(result['actual_usage'],'unknown');self.assertEqual(result['hardware'],'unknown')

    def test_64_stage_trace_keeps_final_save_and_excludes_body(self):
        from dsh_tutor.diagnostics import RequestTrace
        trace=RequestTrace()
        for _ in range(100):trace.mark('model_request')
        trace.mark('save');rows,seconds=trace.finish()
        self.assertEqual(len(rows),64);self.assertEqual(rows[-1]['stage'],'save')
        self.assertGreaterEqual(seconds,0)
