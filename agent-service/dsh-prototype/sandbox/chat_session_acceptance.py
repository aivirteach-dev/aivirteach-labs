"""受限账号下跨进程自动历史问答：虚构代号只来自数据库，模拟工具验证换代次。"""
import argparse
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion,CourseSnapshot
from dsh_tutor.course import build_prompt
from dsh_tutor.runtime import load_config,require_execution_identity
from dsh_tutor.storage import SQLiteChatStore
from sandbox.mcp_acceptance import bridge_for


def source_fingerprint():
    paths=list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))
    paths += [ROOT/name for name in ['config.json','ollama.patch.yml','sandbox/mock_tools.py',
                                    'sandbox/mcp_server.py','sandbox/mcp_acceptance.py',
                                    'sandbox/chat_session_acceptance.py','config/storage.example.json']]
    digest=hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def phase(name,path,token):
    require_execution_identity(load_config(ROOT/'config.json'))
    assert path.parent.parent==ROOT/'sandbox/results/runner-check/chat-session-fixtures'
    scope=dict(student_id='demo-student',learning_context_id='session-demo',course_id='session-demo',
               course_version='v1',lesson_id='l1',instance_id='vm-demo',instance_generation='g1',environment_os='linux')
    course=CourseSnapshot(course_id='session-demo',version='v1',lesson_id='l1',title='虚构对话衔接练习',
        instructions=['练习代号由学生自行提供，不是课程要求或环境事实。', '本课只读模拟服务名称是n8n。'])
    if name=='first':
        store=SQLiteChatStore.create(path)
        handle=store.activate(scope)
        question=f'我为这次练习取的代号是{token}。请仅回复收到，不执行任何操作。'
        rid='session-first'
    else:
        store=SQLiteChatStore(path)
        handle=store.current(student_id='demo-student',learning_context_id='session-demo')
        question='我刚才给这次练习取的代号是什么？只回答代号；前文没提供就说无法确定。'
        rid='session-resume'
    bridge=None
    if name=='switch':
        handle=store.activate(handle.scope.model_copy(update={'instance_id':'vm-new','instance_generation':'g2','environment_os':'windows'}),expected_revision=handle.revision)
        question='请调用检查工具读取n8n当前状态，并明确来源。';rid='session-switch'
        bridge=bridge_for('session-demo','n8n','running',request_id=rid,instance_id='vm-new')
        bridge['context']['policy'].update(learning_context_id='session-demo',lesson_id='l1',instance_generation='g2')
    req=CourseQuestion(request_id=rid,course=course,question=question)
    # resume子进程不接收代号；无历史提示也不包含代号，防止把当前资料当历史恢复。
    bare=build_prompt(req)
    result=answer_chat(req,store=store,handle=handle,config_path=ROOT/'config.json',state_dir=path.parent/'runtime',tool_bridge=bridge)
    return {'reply':result.model_dump(),'pid':os.getpid(),'bare_prompt_sha256':hashlib.sha256(bare.encode()).hexdigest(),
            'bare_prompt':bare if name=='resume' else None,
            'stored_turns':len(store.read_page(handle).turns)}


def main():
    require_execution_identity(load_config(ROOT/'config.json'))
    os.umask(0o077);os.environ['PYTHONDONTWRITEBYTECODE']='1';sys.dont_write_bytecode=True
    parser=argparse.ArgumentParser()
    parser.add_argument('--phase',choices=['first','resume','replay','switch'])
    parser.add_argument('--db');parser.add_argument('--token',default='')
    args=parser.parse_args()
    if args.phase:
        print(json.dumps(phase(args.phase,Path(args.db).resolve(),args.token),ensure_ascii=False))
        return 0
    output=ROOT/'sandbox/results/runner-check'
    fixture=output/'chat-session-fixtures'/uuid4().hex
    fixture.mkdir(parents=True,mode=0o700)
    path=fixture/'fictional-chat.db'
    token='晨星-'+uuid4().hex[:8]
    report={'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            'source_fingerprint':source_fingerprint(),'scope':'虚构学生；3个新模型请求＋1次重放，数据库位于runner私有测试目录；无平台或真实VM接入。',
            'fixture':str(fixture.relative_to(ROOT)),'cases':[]}
    first_pid=None
    resume_answer=None
    for name in ['first','resume','replay','switch']:
        print('开始自动历史问答验收：'+name,flush=True)
        command=[sys.executable,str(Path(__file__).resolve()),'--phase',name,'--db',str(path)]
        if name=='first':command+=['--token',token]
        try:
            child=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=220)
            if child.returncode:
                raise RuntimeError('验收子进程未成功结束。')
            result=json.loads(child.stdout)
            reply=result['reply']
            passed=reply['state']==('replayed' if name=='replay' else 'saved')
            if name=='first':
                first_pid=result['pid'];passed=passed and result['stored_turns']==1 and reply['result']['tool_calls']==0 and reply['result']['context_usage']['used_turns']==0
            elif name=='resume':
                passed=passed and token in (reply['answer'] or '') and result['pid']!=first_pid
                passed=passed and token not in result.pop('bare_prompt') and reply['result']['context_usage']['used_turns']==1
                resume_answer=reply['answer']
            elif name=='replay':
                passed=passed and reply['result'] is None and token in (reply['answer'] or '') and reply['answer']==resume_answer and result['stored_turns']==2
            else:
                value=reply['result'] or {}
                passed=passed and value.get('context_usage',{}).get('used_turns')==0
                passed=passed and value.get('quality',{}).get('facts')=='verified'
                passed=passed and any(x['instance_id']=='vm-new' and x['instance_generation']=='g2' and x['state']=='running' and x['source']=='simulation' for x in value.get('evidence',[]))
            result.pop('bare_prompt',None)
            row={'id':name,'mechanical_check_passed':bool(passed),'output':result}
        except (subprocess.TimeoutExpired,ValueError,RuntimeError,KeyError,TypeError):
            row={'id':name,'mechanical_check_passed':False,'error':'子进程或结果检查失败；未自动重试，远端取消未确认。'}
        report['cases'].append(row)
        (output/'chat-session-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(row,ensure_ascii=False),flush=True)
        if not row['mechanical_check_passed']:
            report['stopped_early']=True;break
    report['source_unchanged']=report['source_fingerprint']==source_fingerprint()
    report['status']='mechanical_checks_passed' if len(report['cases'])==4 and all(x['mechanical_check_passed'] for x in report['cases']) and report['source_unchanged'] else 'needs_review'
    (output/'chat-session-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='mechanical_checks_passed' else 1


if __name__=='__main__':raise SystemExit(main())
