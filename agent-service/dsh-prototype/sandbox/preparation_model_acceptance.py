"""dsh-runner真实模型小样本：有目标/无目标/明确查询早期历史，最多3次串行问答。

只用虚构学生、任务和代号，不访问真实Gateway/VM，不发邮件，不自动重试。
每轮原件在runner私有夹具目录；只有指定交付文件授予runner写权限。
"""
from datetime import datetime,timezone,timedelta
from pathlib import Path
from uuid import uuid4
import hashlib,json,os,re,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.resources import ResourceManagedTutor
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.runtime import load_config,require_execution_identity


def source_fingerprint():
    """模型验收只绑定实际运行代码/模型配置/本入口，区别于全项目回归指纹。"""
    digest=hashlib.sha256()
    for p in sorted(list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))+[ROOT/'config.json',ROOT/'ollama.patch.yml',Path(__file__)]):
        digest.update(p.relative_to(ROOT).as_posix().encode());digest.update(p.read_bytes())
    return digest.hexdigest()


def main():
    require_execution_identity(load_config(ROOT/'config.json'));os.umask(0o077)
    sys.dont_write_bytecode=True;os.environ['PYTHONDONTWRITEBYTECODE']='1'
    # 交付文件由xf创建并仅对本账号授予写权限；缺失不猜测可写其他位置。
    delivery=ROOT/'sandbox/results/runner-check/preparation-report.json'
    if delivery.is_symlink() or not delivery.is_file():raise RuntimeError('delivery_not_prepared')
    delivery_metadata=json.loads(delivery.read_text())
    folder=ROOT/'sandbox/results/runner-check/preparation-fixtures'/uuid4().hex
    folder.mkdir(parents=True,mode=0o700)
    store=SQLiteChatStore.create(folder/'fictional.db');store.migrate_learning_memory();migrate_task_memory(store)
    h=store.activate(dict(student_id='fictional-preparation-student',learning_context_id='fictional-preparation-learning',
        course_id='preparation-demo',course_version='v1',lesson_id='l1',environment_os='linux'))
    repo=TaskRepository(store)
    for task in ('A','B','C'):repo.create(h,task,'虚构任务'+task)
    a=repo.bind(h,'A');b=repo.bind(h,'B');c=repo.bind(h,'C')
    a.edit_learning(h,'goal','搭建新闻简报工作流',expected_revision=0,request_id='goal')
    token='晨星-'+uuid4().hex[:8]
    old=c.begin(h,'old-code','这次练习的代号是'+token+'。');c.finish(h,'old-code',answer='收到。',run_id='seed')
    for i in range(6):c.begin(h,f'filler-{i}',f'记录练习进度{i}。');c.finish(h,f'filler-{i}',answer='收到。',run_id='seed')
    service=ResourceManagedTutor(policy={'min_free_bytes':0},config_path=ROOT/'config.json',state_dir=folder/'runtime')
    report={'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
        'model_source_fingerprint':source_fingerprint(),'regression_source_fingerprint':delivery_metadata['regression_source_fingerprint'],
        'scope':'3个虚构样例，最多3次串行DSH/Ollama；工具0；仅教学核心行为小样本，非模型容量或完整语义保证。',
        'expected':{'goal_keyword':'新闻简报','earlier_code':token},
        'cases':[],'status':'running','original_report_path':str((folder/'report.json').relative_to(ROOT))}
    cases=[('with_memory',a,'我在当前练习中明确记录的学习目标是什么？只复述有效目标，没有记录就明确说没有。',{}),
           ('without_memory',b,'我在当前练习中明确记录的学习目标是什么？只复述有效目标，没有记录就明确说没有。',{}),
           ('earlier_history',c,'我最早给这次练习取的代号是什么？只回答原始代号，前文没有就说无法确定。',{'history_mode':'earlier','history_before':old.sequence+1})]
    for name,bound,text,selection in cases:
        print('开始第二阶段模型样例：'+name,flush=True);started=time.monotonic()
        try:
            q=CourseQuestion(request_id='ask-'+name,question=text,course=dict(course_id='preparation-demo',version='v1',lesson_id='l1',title='虚构记忆读取验收',
                instructions=['教材不提供个人学习目标或练习代号；只能依据当前任务有效记忆及本次前文，不能借用其他任务信息。']))
            result=service.answer(q,store=bound,handle=h,**selection)
            reply=result.chat;answer=(reply.answer or '') if reply else ''
            passed=bool(reply and reply.state=='saved' and reply.result and reply.result.tool_calls==0)
            if name=='with_memory':passed=passed and '新闻简报' in answer
            elif name=='without_memory':passed=passed and '新闻简报' not in answer and bool(re.search('没有|未记录|未提供|未明确|无法确定|不清楚',answer))
            else:passed=passed and token in answer and reply.history.mode=='earlier'
            row={'id':name,'mechanical_check_passed':bool(passed),'managed_reply':result.model_dump(),'total_seconds':round(time.monotonic()-started,3)}
        except Exception:
            row={'id':name,'mechanical_check_passed':False,'error':'样例运行失败；未重试，上游状态须核实。'}
        report['cases'].append(row)
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'id':name,'mechanical_check_passed':row['mechanical_check_passed'],'total_seconds':row.get('total_seconds')},ensure_ascii=False),flush=True)
        if not row['mechanical_check_passed']:break
    report['source_unchanged']=report['model_source_fingerprint']==source_fingerprint()
    report['status']='mechanical_checks_passed' if report['source_unchanged'] and len(report['cases'])==3 and all(x['mechanical_check_passed'] for x in report['cases']) else 'needs_review'
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    delivery.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='mechanical_checks_passed' else 1


if __name__=='__main__':raise SystemExit(main())
