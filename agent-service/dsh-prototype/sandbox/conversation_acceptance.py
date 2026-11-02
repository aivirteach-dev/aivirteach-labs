"""受限身份下串行验证前文指代、同VM状态变化和换VM失效；仅虚构学生与模拟工具。"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dsh_tutor.contracts import CourseQuestion, CourseSnapshot
from dsh_tutor.conversation import ConversationContext
from dsh_tutor.course import answer_course
from dsh_tutor.runtime import load_config, require_execution_identity
from sandbox.mcp_acceptance import bridge_for


def code_fingerprint():
    """覆盖本次运行代码、模型和模拟入口；不读取学生数据或开发测试目录。"""
    paths = list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))
    paths += [ROOT/name for name in ('config.json','ollama.patch.yml','sandbox/mock_tools.py',
                                    'sandbox/mcp_server.py','sandbox/mcp_acceptance.py',
                                    'sandbox/conversation_acceptance.py')]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    require_execution_identity(load_config(ROOT/'config.json'))
    os.umask(0o077)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    output = ROOT/'sandbox/results/runner-check'
    course = CourseSnapshot(course_id='followup-demo', version='v1', lesson_id='l1',
        title='虚构的n8n状态课程', instructions=['这是Linux模拟课程，服务名称为n8n。'])
    current = dict(student_id='demo-student', learning_context_id='demo-enrollment',
        course_id=course.course_id, course_version='v1', lesson_id='l1',
        instance_id='demo-vm', instance_generation='g1', environment_os='linux')
    report = {'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
        'source_fingerprint':code_fingerprint(), 'cases':[],
        'scope':'虚构学生，串行4例；无长期聊天数据库、真实VM、服务器或多人验收。'}
    prior_answer = None
    for case_id in ('text-followup','first-stopped','same-vm-running','new-vm-running'):
        print('开始连续对话验收：'+case_id, flush=True)
        scope = dict(current)
        turns = []
        tool_bridge = None
        if case_id == 'text-followup':
            this_course = course.model_copy(update={'instructions':['本课示例端口是5678；这是虚构课程，不是真实环境观察。']})
            question = '刚才说的端口号是多少？只回答那个数字。'
            turns = [{'scope':scope, 'user':'这个示例使用哪个端口？',
                      'assistant':'本课示例使用端口5678。'}]
        else:
            this_course = course
            question = '请调用工具检查n8n当前状态。'
            if case_id == 'same-vm-running':
                question = '我启动了，帮我再检查一下它。'
                turns = [{'scope':dict(current), 'user':'n8n打不开，请检查它。',
                          'assistant':prior_answer}]
            elif case_id == 'new-vm-running':
                # 当前请求已由应用切换实例；旧内容不能进入模型，尤其不能泄漏标记。
                scope.update(instance_id='demo-new-vm', instance_generation='g2', environment_os='windows')
                turns = [{'scope':dict(current), 'user':'旧机器的n8n状态如何？',
                          'assistant':'OLD_VM_CANARY：旧环境中n8n已停止。'}]
            request_id = 'conversation-'+case_id
            tool_bridge = bridge_for(course.course_id,'n8n',
                'stopped' if case_id == 'first-stopped' else 'running',
                request_id=request_id,instance_id=scope['instance_id'])
            tool_bridge['context']['policy'].update(learning_context_id=scope['learning_context_id'],
                lesson_id=scope['lesson_id'], instance_generation=scope['instance_generation'])
        request_id = 'conversation-'+case_id
        try:
            result = answer_course(CourseQuestion(request_id=request_id, course=this_course, question=question),
                config_path=ROOT/'config.json', state_dir=output/'conversation-runtime',
                tool_bridge=tool_bridge, conversation=ConversationContext.model_validate({'scope':scope,'turns':turns}))
            usage = result['context_usage']
            if case_id == 'text-followup':
                passed = ('5678' in result['answer'] and result['tool_calls']==0 and usage['used_turns']==1)
            else:
                expected = 'stopped' if case_id=='first-stopped' else 'running'
                passed = (result['tool_calls']>=1 and result['quality']['facts']=='verified'
                          and result['quality']['explanation']=='validated_selection'
                          and any(e['state']==expected and e['source']=='simulation'
                                  and e['instance_id']==scope['instance_id']
                                  and e['instance_generation']==scope['instance_generation'] for e in result['evidence']))
                if case_id == 'first-stopped':
                    prior_answer = result['answer']
                if case_id == 'same-vm-running':
                    passed = passed and usage['used_turns']==1 and '已停止' not in result['answer']
                if case_id == 'new-vm-running':
                    passed = passed and usage['used_turns']==0 and usage['discarded_scope']==1 and 'OLD_VM_CANARY' not in result['answer']
            row = {'id':case_id,'mechanical_check_passed':bool(passed),'result':result,
                   'semantic_review':'待开发复核；小样本不证明所有指代、任意语义或真实环境正确。'}
        except Exception as exc:
            row = {'id':case_id,'mechanical_check_passed':False,'error':str(exc)}
        report['cases'].append(row)
        (output/'conversation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(row,ensure_ascii=False),flush=True)
        if 'error' in row or not row['mechanical_check_passed']:
            # 不自动重试或继续堆入请求；本机超时不证明远端推理已经取消。
            report['stopped_early'] = True
            break
    report['source_unchanged'] = report['source_fingerprint']==code_fingerprint()
    report['status'] = 'mechanical_checks_passed' if report['source_unchanged'] and len(report['cases'])==4 and all(
        x['mechanical_check_passed'] for x in report['cases']) else 'needs_review'
    (output/'conversation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='mechanical_checks_passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
