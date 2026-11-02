"""受限账号的真实模型验收：两个虚构课程，共用同一个工具接口。"""
import json
import os
import sys
import hashlib
import argparse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dsh_tutor.runtime import load_config, require_execution_identity
from dsh_tutor.contracts import CourseSnapshot, CourseQuestion
from dsh_tutor.course import answer_course
from dsh_tutor.tools import TOOL_NAME


def bridge_for(course_id, service, state, *, request_id, instance_id):
    """模拟资料由演示调用方提供；正式适配器不读取 sandbox。"""
    return {'entry_module': 'sandbox.mcp_server', 'server_name': 'course_tools',
            'context': {'policy': {'request_id': request_id, 'student_id': 'demo-student',
                                   'instance_id': instance_id, 'course_id': course_id,
                                   'course_version': 'v1', 'allowed_tools': [TOOL_NAME],
                                   'allowed_services': [service], 'max_attempts': 3,
                                   'timeout_seconds': 2.0}, 'service_states': {service: state}}}


def code_fingerprint():
    """只读取runner已获准的运行源码；不为验收开放开发测试目录。"""
    paths=list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))
    paths += [ROOT/name for name in ['config.json','ollama.patch.yml','sandbox/mock_tools.py',
                                    'sandbox/mcp_server.py','sandbox/mcp_acceptance.py']]
    digest=hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--course',choices=['all','n8n-demo','codex-demo'],default='all')
    selected=parser.parse_args().course
    config=load_config(ROOT/'config.json')
    require_execution_identity(config)
    os.umask(0o077)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    sys.dont_write_bytecode=True
    source_fingerprint=code_fingerprint()
    output=ROOT/'sandbox/results/runner-check'
    report={'at':datetime.now(timezone.utc).isoformat(),'cases':[], 'source_fingerprint':source_fingerprint,
            'selected_course': selected,
            'scope':'按cases记录实际测试课程；验证模拟工具事实展示与受限说明，不含真实VM、Gateway或多人容量。'}
    for course_id,title,service,state,environment in [
        ('n8n-demo','n8n 入门实验','n8n','stopped','Linux'),
        ('codex-demo','AI 助手工具实验','assistant','running','Windows')]:
        if selected!='all' and selected!=course_id:
            continue
        request_id='mcp-'+course_id
        course=CourseSnapshot(course_id=course_id,version='v1',lesson_id='lesson-1',title=title,
                              goals=['确认课程服务状态'],instructions=[f'这是一份虚构的{environment}实验；服务名称是{service}。'])
        request=CourseQuestion(request_id=request_id,course=course,
                               question=f'请调用检查工具读取服务 {service} 的状态，并说明是否为模拟结果。')
        print('开始测试：'+title,flush=True)
        try:
            result=answer_course(request,config_path=ROOT/'config.json',state_dir=output/'mcp-runtime',
                                 tool_bridge=bridge_for(course_id,service,state,request_id=request_id,
                                                        instance_id='simulated-'+environment.lower()))
            evidence=result.get('evidence',[])
            # 成功需要真实工具入口记录；文字声称调用过不算通过。
            passed=(result['tool_calls']>=1 and any(x['service']==service and x['state']==state
                    and x['source']=='simulation' for x in evidence))
            expected_text={'running':'正在运行','stopped':'已停止','unknown':'暂时无法确定'}[state]
            facts_passed=(passed and result['quality']['facts']=='verified'
                          and expected_text in result['answer'] and '模拟检查，未访问真实实验机' in result['answer'])
            explanation_passed=result['quality']['explanation']=='validated_selection'
            row={'course':course_id,'tool_chain_passed':passed, 'facts_passed':facts_passed,
                 'explanation_selection_passed':explanation_passed, 'result':result,
                 'semantic_review':'受限说明无自由改写，课程引用相关性仍由Codex另行复核；不代表任意语义已自动验证。'}
        except Exception as exc:
            row={'course':course_id,'tool_chain_passed':False,'error':str(exc)}
        report['cases'].append(row)
        # 文件预先由xf创建，精确授予runner写入；结果保持可被开发账号核对。
        (output/'quality-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(row,ensure_ascii=False),flush=True)
    report['source_unchanged']=code_fingerprint()==source_fingerprint
    report['status']='passed' if report['source_unchanged'] and report['cases'] and all(
        x['tool_chain_passed'] and x.get('facts_passed') and x.get('explanation_selection_passed')
        for x in report['cases']) else 'needs_review'
    (output/'quality-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
