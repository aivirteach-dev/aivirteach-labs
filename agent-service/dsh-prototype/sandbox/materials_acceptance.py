"""真实教材的小样本模型验收；无工具、无外部课程操作、无学生历史存储。"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime,timezone,timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from dsh_tutor.runtime import load_config,require_execution_identity
from dsh_tutor.materials import MaterialRepository
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.course import answer_course


def code_fingerprint():
    """覆盖本轮实际运行代码、模型配置和教材，不依赖测试目录读权限。"""
    paths=list((ROOT/'dsh_tutor').rglob('*.py'))+list((ROOT/'materials/courses').rglob('*.json'))
    paths += [ROOT/name for name in ('config.json','ollama.patch.yml','config/materials.json','sandbox/materials_acceptance.py')]
    digest=hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    require_execution_identity(load_config(ROOT/'config.json'))
    os.umask(0o077)
    os.environ['PYTHONDONTWRITEBYTECODE']='1';sys.dont_write_bytecode=True
    config=json.loads((ROOT/'config/materials.json').read_text())
    repo=MaterialRepository(ROOT/'materials/courses')
    output=ROOT/'sandbox/results/runner-check'
    report={'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
            'source_fingerprint':code_fingerprint(),'course_id':config['course_id'],
            'version':config['version'],'cases':[],
            'scope':'两种语言的真实教材、三个无工具问答样例；未执行教材操作，没有聊天历史存储。'}
    cases=[
        ('zh-port','zh-CN','lesson-1-2-install-and-start-n8n',
         '根据本课教材，应该在Ubuntu浏览器里打开哪个地址访问n8n？只回答地址，不执行命令。', 'http://localhost:5678'),
        ('en-timezone','en','lesson-3-2-customize-delivery-and-test',
         'Which timezone does this course currently use for n8n and the Code node? Answer with the timezone identifier only.', 'Asia/Kuala_Lumpur'),
        ('zh-missing','zh-CN','lesson-1-2-install-and-start-n8n',
         '教材有没有提供当前学生虚拟机的实际内存容量？没有就明确说当前资料未提供，不要猜测。', None)]
    for case_id,locale,lesson,question,expected in cases:
        print('开始教材验收：'+case_id,flush=True)
        snapshot=repo.prepare(config['course_id'],config['version'],locale,lesson,question)
        row={'id':case_id,'locale':locale,'lesson_id':lesson,'input_snapshot':snapshot.model_dump()}
        try:
            result=answer_course(CourseQuestion(request_id='material-'+case_id,course=snapshot,question=question),
                                 config_path=ROOT/'config.json',state_dir=output/'materials-runtime')
            text=result['answer']
            content_ok=(expected in text if expected else bool(re.search('未提供|未说明|没有提供|未给出',text))
                        and not re.search(r'\d+\s*(?:GB|GiB|G内存)',text,re.I))
            row.update(result=result,mechanical_check_passed=bool(content_ok and result['tool_calls']==0),
                       semantic_review='待Codex复核；关键词匹配不能证明任意回答正确。')
        except Exception as exc:
            row.update(error=str(exc),mechanical_check_passed=False)
        report['cases'].append(row)
        (output/'materials-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({key:value for key,value in row.items() if key!='input_snapshot'},ensure_ascii=False),flush=True)
        if 'error' in row:
            # 本机结束不证明远端已取消；运行失败后不继续向Mac排入下一题。
            report['stopped_early']=True
            break
    report['source_unchanged']=report['source_fingerprint']==code_fingerprint()
    report['status']='mechanical_checks_passed' if report['source_unchanged'] and len(report['cases'])==len(cases) and all(x['mechanical_check_passed'] for x in report['cases']) else 'needs_review'
    (output/'materials-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='mechanical_checks_passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
