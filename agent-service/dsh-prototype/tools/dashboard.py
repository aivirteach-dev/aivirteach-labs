"""构建离线开发看板，或通过本机只读服务查看文件更新。"""
import argparse
import base64
import hashlib
import html
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from architecture_report import render_report, REPORT_CSS, REPORT_SCRIPT, diagram

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "development"
OUT = ROOT / "dashboard" / "index.html"


def fingerprint():
    """测试绑定代码和配置；文档修改不会让测试无意义失效。"""
    paths = list((ROOT / "dsh_tutor").rglob("*.py")) + list((ROOT/'dsh_tutor/plugins').glob('*.mjs')) + list((ROOT/'sandbox/tests').glob('*.mjs')) + list((ROOT / "sandbox/tests").rglob("*.py"))
    paths += list((ROOT/'management_frontend/app').rglob('*.tsx')) + list((ROOT/'management_frontend/app').rglob('*.ts'))
    paths += [ROOT/name for name in ('management_frontend/package.json','management_frontend/package-lock.json','tools/start_management.py','tools/serve_management.py','sandbox/management_seed.py')]
    paths += [ROOT/name for name in ('sandbox/scheduling_demo.py','sandbox/scheduling_acceptance.py')]
    paths += [ROOT/name for name in ('sandbox/preparation_acceptance.py','sandbox/preparation_model_acceptance.py')]
    paths += [ROOT/n for n in ('sandbox/completion_demo.py','sandbox/completion_acceptance.py','tools/maintain_service.py','config/service-maintenance.example.json')]
    paths += [ROOT/n for n in ('sandbox/low_cost_demo.py','sandbox/low_cost_acceptance.py','tools/publish_materials.py','config/material-cache.example.json')]
    paths += [ROOT/n for n in ('sandbox/efficiency_acceptance.py','sandbox/efficiency_demo.py','sandbox/efficiency_fixture.py','tools/check_execution_profile.py')]
    paths += [ROOT/name for name in ('sandbox/integration_fixture.py','sandbox/integration_client.py',
        'sandbox/integration_crash_worker.py','sandbox/integration_demo.py','sandbox/integration_acceptance.py',
        'config/service.example.json','tools/check_service_contract.py')]
    paths += [ROOT/name for name in ('sandbox/inspection_fixtures.py','sandbox/inspection_demo_seed.py','sandbox/inspection_acceptance.py')]
    paths += [ROOT/name for name in ('sandbox/lifecycle_admin.py','sandbox/lifecycle_acceptance.py','config/lifecycle.example.json')]
    paths += [ROOT / name for name in ('sandbox/task_memory_demo.py','sandbox/task_memory_demo.html','sandbox/task_memory_acceptance.py')]
    paths += [ROOT/n for n in ('sandbox/runtime_optimization_acceptance.py','sandbox/warm_probe.mjs','config/provider.example.json')]
    paths += [ROOT / "config.json", ROOT / "ollama.patch.yml"]
    paths += [ROOT / name for name in ('sandbox/mock_tools.py', 'sandbox/mcp_server.py', 'sandbox/mcp_acceptance.py')]
    paths += [ROOT / name for name in ('sandbox/materials_acceptance.py','sandbox/conversation_acceptance.py','sandbox/storage_acceptance.py','sandbox/chat_session_acceptance.py','sandbox/learning_memory_acceptance.py','sandbox/resource_acceptance.py','config/resources.example.json','config/storage.example.json','config/materials.json','tools/import_materials.py')]
    paths += list((ROOT/'materials/courses').rglob('*.json')) + list((ROOT/'inputs/courses').rglob('*.html'))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_data():
    status = read_json(DEV / "status.json")
    audit = read_json(DEV / "github-review.json")
    tests = read_json(DEV / "latest-tests.json") if (DEV / "latest-tests.json").exists() else None
    current = fingerprint()
    material = read_json(DEV / "materials-import-20261029.json") if (DEV / "materials-import-20261029.json").exists() else None
    conversation = read_json(DEV / "conversation-acceptance-20261030.json") if (DEV / "conversation-acceptance-20261030.json").exists() else None
    payload = {"status": status, "audit": audit, "tests": tests, "fingerprint": current, "materials": material, "conversation": conversation}
    payload["revision"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload


def esc(value):
    return html.escape(str(value), quote=True)


def rows(values):
    return "".join("<tr>" + "".join("<td>" + esc(x) + "</td>" for x in row) + "</tr>" for row in values)


def table(headers, values):
    return '<div class="table-wrap"><table><thead><tr>' + ''.join('<th>' + esc(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + rows(values) + '</tbody></table></div>'


def render_runtime_optimization(record):
    if not record:return ''
    return ('<section id="efficiency-stage4" class="searchable"><h2>效率优化④ · 模型接入、取消与空闲控制</h2>'+
        '<span class="badge">本地规则与固定假服务协议通过；真实收益待服务器</span><p>'+esc(record['summary'])+'</p>'+
        '<pre>'+esc(record['flow'])+'</pre>'+table(['本轮完成','行为与边界'],record['completed'])+
        '<p>'+esc(record['validation'])+'</p><div class="notice">'+esc(record['boundary'])+'</div></section>')


def render_completion(record):
    if not record:return ''
    return ('<section id="efficiency-stage3" class="searchable"><h2>效率优化③ · 模型与保存分段、记录与目录维护</h2>'+
        '<span class="badge">本地功能与协议验收通过；真实收益待服务器</span><p>'+esc(record['summary'])+'</p>'+
        '<pre>'+esc(record['flow'])+'</pre>'+table(['本轮能力','已验证行为与边界'],record['completed'])+
        '<p>'+esc(record['validation'])+'</p><div class="notice">'+esc(record['boundary'])+'</div></section>')


def render_low_cost(record):
    if not record:return ''
    return ('<section id="efficiency-stage2" class="searchable"><h2>效率优化② · 教材预处理与低成本请求</h2>'+
        '<span class="badge">本地功能与协议验收通过；真实效果待服务器</span><p>'+esc(record['summary'])+'</p>'+
        '<pre>'+esc(record['flow'])+'</pre>'+table(['本轮能力','行为与边界'],record['completed'])+
        '<p>验证：'+esc(record['validation'])+'</p><div class="notice">'+esc(record['boundary'])+'</div></section>')


def render_efficiency(record):
    """新效率路线独立记录，避免与此前已完成的接入前三阶段混淆。"""
    if not record:return ''
    return ('<section id="efficiency-stage1" class="searchable"><h2>效率优化① · 计时、进度和总预算</h2>'+
        '<span class="badge">本地功能与协议验收通过；真实模型/服务器效果待验证</span><p>'+esc(record['summary'])+'</p>'+
        table(['本轮能力','已验证行为'],record['completed'])+
        '<h3>五阶段安排</h3>'+table(['阶段','范围','状态'],record['roadmap'])+
        '<div class="notice">'+esc(record['boundary'])+'</div></section>')


def render_architecture(plan):
    """状态文件描述架构；所有文字统一转义，流程离线可读，不依赖外网绘图库。"""
    if not plan:
        return ''
    pieces = ['<section id="architecture"><h2>早期架构设计存档 · 2026-10-30</h2><p>' + esc(plan['intro']) +
              '</p><p class="muted">方案记录：' + esc(plan['recorded_at']) +
              '。架构说明归档不代表功能已经开发。绿色标记表示当前受限验证范围，虚线卡片表示目标设计。</p><nav class="architecture-nav" aria-label="架构章节">']
    for item in plan['flows'] + plan['tables']:
        pieces.append('<a href="#' + esc(item['id']) + '">' + esc(item['title']) + '</a>')
    pieces.append('</nav></section>')
    for index, flow in enumerate(plan['flows']):
        pieces.append('<section class="architecture-section searchable" id="' + esc(flow['id']) +
                      '"><h2>' + esc(flow['title']) + '</h2><span class="badge">' + esc(flow['status']) +
                      '</span><p>' + esc(flow['description']) + '</p><ol class="architecture-flow' +
                      ('' if flow.get('implemented',index==0) else ' architecture-planned') + '">')
        for title, detail in flow['steps']:
            pieces.append('<li><strong>' + esc(title) + '</strong><p>' + esc(detail) + '</p></li>')
        pieces.append('</ol><div class="notice">' + esc(flow['boundary']) + '</div></section>')
    for item in plan['tables']:
        pieces.append('<section class="architecture-section searchable" id="' + esc(item['id']) +
                      '"><h2>' + esc(item['title']) + '</h2>' + table(item['headers'],item['rows']) + '</section>')
    pieces.append('<section id="architecture-sources"><h2>方案参考资料</h2><p>资料用于设计参考；不代表已安装对应框架，也不是本项目验收证据。</p><ul>')
    for title,url in plan.get('sources',[]):
        pieces.append('<li><a target="_blank" rel="noopener" href="' + esc(url) + '">' + esc(title) + '</a></li>')
    pieces.append('</ul></section>')
    return ''.join(pieces)


def render_storage(record):
    """存储阶段的脱敏证据和边界由status维护，避免把测试正文展示到看板。"""
    if not record:
        return ''
    return ('<section id="storage" class="searchable"><h2>长期记忆① · 可靠保存与恢复</h2><span class="badge">第一步已完成 · 独立存储验收</span>' +
            '<p>' + esc(record['summary']) + '</p>' +
            table(['验收场景','结果','样例编号'],record['case_reviews']) +
            '<h3>当前资源限制</h3>' + table(['项目','初始配置与边界'],record['limits']) +
            '<h3>四步路线</h3>' + table(['步骤','范围','状态'],record['roadmap']) +
            '<div class="notice">' + esc(record['boundary']) + '</div></section>')


def render_chat_session(record):
    """第二步规则和模型结果独立展示，不把自动模拟测试当作模型验收。"""
    if not record:
        return ''
    return ('<section id="persistent-chat" class="searchable"><h2>长期记忆② · 自动衔接连续问答</h2><p>' +
            esc(record['summary']) + '</p>' + table(['项目','行为与边界'],record['rules']) +
            (table(['样例','状态','实际回答','模型耗时（秒）'],record['model_checks']) if record.get('model_checks') else
             '<div class="notice">实际模型跨进程验收待受限运行；不能将模拟测试视为模型语义通过。</div>') +
            '<p>虚构学生与模拟工具；平台、摘要、生产并发待做。接口见docs/persistent-chat.md。</p></section>')


def render_learning_memory(record):
    """学习记忆阶段单独展示；原始学生记忆不写入通用开发看板。"""
    if not record:
        return ''
    return ('<section id="learning-memory" class="searchable"><h2>长期记忆③ · 学习过程与摘录摘要</h2><p>' +
            esc(record['summary']) + '</p>' + table(['项目','实现和边界'],record['rules']) +
            (table(['样例','状态','回答','耗时（秒）'],record['model_checks']) if record.get('model_checks') else
             '<div class="notice">三例实际模型验收待运行；自动模拟测试不证明模型理解已通过。</div>') +
            '<p>学生原文四前缀受控接入；不是自动推断学生画像或知识掌握。文档docs/learning-memory.md。</p></section>')


def render_resource_controls(record):
    """本地指标快照与真实平台缺口分开展示，不伪造生产监控。"""
    if not record:
        return ''
    return ('<section id="resource-controls" class="searchable"><h2>长期记忆④ · 本地资源控制与平台准备</h2><p>' +
            esc(record['summary']) + '</p>' + table(['项目','行为与边界'],record['rules']) +
            '<h3>本地验收 · 非模型吞吐压测</h3>' + table(['场景','结果'],record['case_rows']) +
            '<h3>真实平台仍待完成</h3>' + table(['项目','缺口'],record['platform_gaps']) +
            '<div class="notice">服务器尚未准备，第四步整体未完成。此处是开发验收快照，不是生产实时监控。</div></section>')


def render_task_memory(record):
    """开发快照只展示虚构验证范围；交互演示与只读看板分开。"""
    if not record:
        return ''
    return ('<section id="task-memory" class="searchable"><h2>任务记忆与学生管理 · 独立原型</h2><p>' +
            esc(record['summary']) + '</p>' + table(['项目','实现与限制'],record['rules']) +
            '<h3>浏览器核对</h3>' + table(['操作','结果'],record['ui_cases']) +
            (table(['实际模型场景','复核','实际回答','耗时（秒）'],record['model_checks']) if record.get('model_checks') else '<p>实际模型复核待运行。</p>') +
            '<details><summary>查看虚构管理页截图</summary><img src="data:image/png;base64,' + base64.b64encode((ROOT/'dashboard/task-memory-preview.png').read_bytes()).decode('ascii') + '" alt="虚构任务记忆管理页" style="max-width:100%;height:auto"></details>' +
            '<p><a href="http://127.0.0.1:8790/" target="_blank" rel="noopener">打开虚构记忆管理演示</a> · 仅在Ubuntu演示与Windows SSH转发运行时可用。</p>' +
            '<p>详情docs/task-memory.md；任意语义质量、正式平台、跨任务检索、证据持久化和数据归档仍待做。</p></section>')


def render_lifecycle(record):
    """维护与诊断快照仅虚构范围；与学生交互页面/生产监控分开。"""
    if not record:
        return ''
    preview=base64.b64encode((ROOT/'dashboard/lifecycle-preview.png').read_bytes()).decode('ascii')
    return ('<section id="lifecycle" class="searchable"><h2>生命周期、诊断与本地维护</h2><p>' + esc(record['summary']) + '</p>' +
            table(['模块','行为和边界'],record['rules']) + '<h3>组合验收 · 真实SQLite / 模型替身</h3>' + table(['场景','结果'],record['cases']) +
            '<h3>固定虚构管理库 · 容量快照</h3><p>采集时间：' + esc(record['snapshot_at']) + '。仅已配置库/维护目录，非全盘或增长预测。</p>' +
            table(['类别','字节数'],record['capacity_rows']) + '<h3>隔离组合夹具 · 诊断样例</h3>' +
            table(['请求哈希引用','阶段','状态','原因码','应用耗时（秒）'],record['event_rows']) +
            '<p>以上含替身故障注入，不是线上故障或远端GPU耗时；滚动诊断不是完整审计。管理演示库没有实际问答，所以其诊断数为0。</p>' +
            '<h3>管理员独立恢复验证</h3><pre>' + esc(json.dumps(record['restore_validation'],ensure_ascii=False,indent=2)) + '</pre>' +
            '<h3>页面核对</h3>' + table(['操作','结果'],record['ui_cases']) +
            '<details><summary>查看虚构任务归档界面截图</summary><img style="max-width:100%;height:auto" alt="虚构任务归档界面" src="data:image/png;base64,' + preview + '"></details>' +
            '<p>说明docs/lifecycle-maintenance.md；这是开发验收快照，不是生产实时监控。</p></section>')


def render_preparation(record):
    """本轮验收单独展示，模型文案问题与程序检查不能混为全部质量通过。"""
    if not record:return ''
    review=record['model_review']
    return ('<section id="preparation-stage2" class="searchable"><h2>第二阶段记录：资料准备与记忆读取</h2>' +
        '<span class="badge">第二阶段本地范围完成 · 真实模型核心复核通过</span>' +
        '<p>308项自动检查（新增24）、11项第二阶段组合断言、12项第一阶段组合断言及前端构建/页面核对通过。'+
        '资料准备和DSH执行分开；仍限制总请求数量，Mac仍并发1。</p>' +
        table(['已实现','说明'],[(x,'已本地验证；生产边界见下方') for x in record['completed']]) +
        '<h3>真实Qwen三例 · 保存与核心行为复核</h3>' +
        table(['场景','实际回答','准备耗时（秒）','完整耗时（秒）'],
            [(x['core_behavior'],x['answer'],x['preparation_seconds'],x['total_seconds']) for x in review['case_reviews']]) +
        '<div class="notice">质量保留项：'+esc(' '.join(review['quality_notes']))+'<br>'+esc(review['latency_limit'])+'</div>' +
        '<details><summary>模块说明、首次报告路径修复和待验证内容</summary><p>说明：docs/preparation-stage2.md。'+
        '原始模型报告与人工复核分别保留，不改写原件。</p><p>'+esc(review['first_attempt'])+'</p><ul>'+
        ''.join('<li>'+esc(x)+'</li>' for x in record['remaining'])+'</ul></details></section>')


def render_integration(record):
    """第三阶段当前成果；原模型样例仍保留为历史证据，不冒充本轮调用。"""
    if not record:return ''
    return ('<section id="integration-stage3"><h2>第三阶段完成 · 接入与故障准备</h2>'+
        '<span class="badge">本地范围通过 · '+esc(record['date'])+'</span><p>'+esc(record['summary'])+'</p>'+
        '<div class="diagram-scroll">'+diagram({'title':'交付与恢复：本地当前流程','text':record['flow']})+'</div>'+
        '<p>全量 '+esc(record['test_count'])+' 项检查；本轮新增 '+esc(record['new_tests'])+' 项；'+
        esc(record['combination_checks'])+' 项组合断言。'+esc(record['validation_note'])+'</p>'+
        table(['已完成','作用'],record['completed'])+
        '<details><summary>固定接口、故障恢复与验证边界</summary><p>'+esc(record['api_note'])+'</p><ul>'+
        ''.join('<li>'+esc(x)+'</li>' for x in record['remaining'])+'</ul><p>说明：docs/integration-stage3.md；'+
        '证据：development/integration-stage3-20261031.json；源码指纹：'+esc(record['source_fingerprint'])+'</p></details></section>')


def render(data):
    """单一状态数据生成离线/在线页面；转义所有资料，避免插入可执行内容。"""
    s, a, t = data["status"], data["audit"], data["tests"]
    test_status = "尚未执行" if not t else ("通过" if t["exit_code"] == 0 else "失败")
    stale = bool(t and t["source_fingerprint"] != data["fingerprint"])
    if stale:
        test_status += "（代码已变更，需重跑）"
    completed = s.get("completed_work", [])
    completed_html = ('<section id="completed"><h2>已经完成的工作</h2>'
                      '<p>以下逐项记录实际完成范围；不把环境就绪当作 Agent 功能全部完成。</p>'
                      '<div class="cards">' + ''.join(
                          '<article class="card searchable"><span class="badge">已完成 · ' + esc(x['date']) +
                          '</span><h3>' + esc(x['title']) + '</h3><p>' + esc(x['result']) +
                          '</p><small>验证依据：' + esc(x['evidence']) + '</small></article>'
                          for x in completed) + '</div><div class="notice">' +
                      esc(s.get('next_work', '下一步待确定')) + '</div></section>')
    isolation_html = ('<section id="isolation"><h2>隔离与权限检查 · 本轮基线已核对</h2><p>检查时间：' +
                      esc(s.get('isolation_checked_at', '未记录')) +
                      '。各项记录实际验证范围；基础加固不代表完整安全验收。</p>' +
                      table(['检查项', '结果', '依据与边界'], s.get('isolation_checks', [])) + '</section>') if s.get('isolation_checks') else ''
    material = data.get('materials')
    material_html = ''
    if material:
        material_html = ('<section id="materials"><h2>真实教材 · 双语接入</h2><p>课程：' + esc(material['course_id']) +
                         ' · 本地导入版本：' + esc(material['version']) +
                         '。平台正式发布版本未确认。每种语言15个内容单元（含概览），35个代码块一致。</p>' +
                         '<p>模型问答状态：' + esc(material.get('model_acceptance','pending')) + '</p>' +
                         (table(['问答样例','语言','实际回答','耗时（秒）','工具调用'],
                                [(x['id'],x['locale'],x['answer'],x['elapsed_seconds'],x['tool_calls'])
                                 for x in material.get('model_checks',[])]) if material.get('model_checks') else '') +
                         table(['稳定课时ID','中文标题','英文标题','代码核对'],
                               [(x['lesson_id'],x['zh-CN'],x['en'],'一致' if x['code_blocks_equal'] else '有差异')
                                for x in material['lessons']]) +
                         '<details><summary>查看原件SHA256</summary><pre>' + esc(json.dumps(material['source_sha256'],ensure_ascii=False,indent=2)) +
                         '</pre></details><p>原件和全量提取资料保留；模型只接收当前课时的有限摘录。图片未识别，未执行教材操作，未增加长期聊天保存。</p></section>')
    conversation = data.get('conversation')
    conversation_html = ''
    if conversation:
        conversation_html = ('<section id="conversation"><h2>连续对话与动态环境</h2><p>' + esc(conversation['summary']) + '</p>' +
            table(['规则','当前行为'], conversation['rules']) +
            (table(['模型样例','实际回答','耗时（秒）','保留前文轮次','省略旧范围轮次'],
                [(x['id'],x['answer'],x['elapsed_seconds'],x['used_turns'],x['discarded_scope']) for x in conversation.get('model_checks',[])])
             if conversation.get('model_checks') else '<p>实际模型四例：待受限账号运行；不能把自动模拟检查当成模型理解已验收。</p>') +
            '<p>聊天存储第一步已完成，见下节；本历史模型样例未自动加载数据库。未接真实身份、VM或多人服务。详见docs/conversation-context.md。</p></section>')
    stages = ''.join('<article class="card searchable"><span class="badge">' + esc(x['status']) + '</span><h3>' + esc(x['title']) + '</h3><p>' + esc(x['done']) + '</p><small>进入下一阶段的条件：' + esc(x['gate']) + '</small></article>' for x in s['stages'])
    findings = ''.join('<article class="finding searchable"><span class="badge">' + esc(x['priority']) + '</span><h3>' + esc(x['title']) + '</h3><p>' + esc(x['detail']) + '</p><a target="_blank" rel="noopener" href="' + esc(x['url']) + '">源码依据 · ' + esc(x['path']) + '</a></article>' for x in a['findings'])
    risks = ''.join('<li>' + esc(x) + '</li>' for x in s['risks'])
    decisions = ''.join('<li>' + esc(x) + '</li>' for x in s['decisions'])
    commits = table(['仓库','固定 main 提交'],[(repo, sha) for repo,sha in a['commits'].items()])
    tests_html = '<p>最近执行：' + esc(t['at'] if t else '未执行') + ' · ' + esc(test_status) + '</p><details><summary>查看测试原始输出</summary><pre>' + esc(t['output'] if t else '无记录') + '</pre></details>'
    requirement_table = table(['约定','设计方式','验收/边界'], s['requirements'])
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DSH × AIVirTeach 开发看板</title><style>
:root{--nav:#142e35;--paper:#f5f4ef;--ink:#20363a;--muted:#5b6f73;--line:#d7e0da;--accent:#176b5c}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.8 "Microsoft YaHei",sans-serif}aside{position:fixed;inset:0 auto 0 0;width:230px;background:var(--nav);padding:30px 23px;color:#d9e9e3;overflow:auto}aside a{display:block;color:#d9e9e3;text-decoration:none;padding:8px 0}aside strong{display:block;font-size:20px;margin-bottom:22px}main{margin-left:230px;max-width:1420px;padding:38px 44px}h1{font-size:clamp(27px,4vw,40px);margin:10px 0}h2{font-size:24px;margin-top:46px;border-bottom:1px solid var(--line);padding-bottom:12px}h3{font-size:17px;margin:10px 0}p{margin:10px 0}a{color:var(--accent)}.muted,small{color:var(--muted)}.notice{border-left:4px solid #b57c38;background:#fff2dc;padding:15px 20px;margin:20px 0}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card,.finding{background:white;border:1px solid var(--line);border-radius:10px;padding:21px}.finding{margin:14px 0}.badge{display:inline-block;border-radius:20px;padding:3px 10px;background:#e7f1ed;color:#205b50;font-size:12px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;background:white}td,th{padding:13px 15px;text-align:left;border:1px solid var(--line);vertical-align:top}th{background:#e9eeea}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#e9eeea;padding:18px;border-radius:6px;font:13px/1.7 Consolas,monospace}input{width:100%;max-width:500px;padding:11px;border:1px solid var(--line);border-radius:6px}button{padding:9px 16px;background:var(--accent);color:white;border:0;border-radius:6px;cursor:pointer}li{margin:9px 0}details summary{cursor:pointer;color:var(--accent)}[hidden]{display:none!important}@media(max-width:850px){aside{position:relative;width:100%;padding:18px}aside a{display:inline-block;margin-right:15px}main{margin-left:0;padding:20px}.cards{grid-template-columns:1fr}table{min-width:650px}}@media print{aside,input,button{display:none}main{margin:0;padding:0}}
.architecture-nav{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}.architecture-nav a{border:1px solid var(--line);border-radius:7px;padding:6px 10px;background:white;text-decoration:none}.architecture-flow{list-style:none;counter-reset:arch;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px;padding:0;margin:20px 0}.architecture-flow li{counter-increment:arch;margin:0;padding:20px;background:#fff;border:1px solid #97b6a7;border-radius:9px;min-width:0}.architecture-flow li:before{content:counter(arch);display:inline-flex;align-items:center;justify-content:center;width:27px;height:27px;background:#e7f1ed;border-radius:50%;color:#205b50;margin-right:9px;font-weight:bold}.architecture-flow p{font-size:14px}.architecture-planned li{border-style:dashed;border-color:#b0a188;background:#fffdf7}.architecture-section{scroll-margin-top:20px}.architecture-section td{overflow-wrap:anywhere}@media(max-width:850px){.architecture-flow{grid-template-columns:1fr}}@media print{.architecture-nav{display:none}.architecture-flow{display:block}.architecture-flow li{break-inside:avoid;margin:10px 0}}
__REPORT_CSS__</style></head><body><aside><strong>DSH / AIVirTeach</strong><p>开发与验证记录</p><a href="#progress-view" data-view="progress">开发进度</a><a href="#team-report" data-view="report">项目怎么运行</a><div class="report-sidebar"><a href="#report-platform">平台架构</a><a href="#report-agent">助教怎样回答</a><a href="#report-memory">怎样记住学生</a><a href="#report-integration">管理员怎么看</a><a href="#report-delivery">完成度与交接</a></div><div class="progress-nav"><a href="#efficiency-stage4">本轮：模型接入与取消保护</a><a href="#efficiency-stage3">效率③：分段保存与维护</a><a href="#efficiency-stage2">效率②：教材与低成本请求</a><a href="#efficiency-stage1">效率①：计时与预算</a><a href="#integration-stage3">接入与故障</a><a href="#preparation-stage2">第二阶段：资料与记忆</a><a href="#isolation">隔离检查结果</a><a href="#completed">已完成的工作</a><a href="#materials">真实双语教材</a><a href="#conversation">连续对话</a><a href="#storage">聊天存储第一步</a><a href="#persistent-chat">自动历史问答</a><a href="#learning-memory">学习记忆第三步</a><a href="#resource-controls">资源控制第四步</a><a href="#task-memory">任务记忆与管理</a><a href="#lifecycle">生命周期与维护</a><a href="#architecture">架构总览</a><a href="#architecture-memory">长期记忆设计</a><a href="#architecture-resource-flow">资源控制设计</a><a href="#direction">方向与约定</a><a href="#stages">阶段进度</a><a href="#audit">GitHub 源码核查</a><a href="#tests">测试证据</a><a href="#deployment">AIVirTeach 部署代价</a><a href="#risks">未解决事项</a><a href="#use">更新方式</a></div></aside><main><nav class="view-switch" aria-label="页面切换"><a href="#progress-view" data-view="progress">开发进度</a><a href="#team-report" data-view="report">项目怎么运行</a></nav>__TEAM_REPORT__<div id="progress-view">
<span class="badge">独立原型 · 尚未接入 AIVirTeach</span><h1>开发方向、需求与测试看板</h1><p class="muted">状态更新：__UPDATED__ · 页面生成：__NOW__</p><p id="live" class="muted">离线快照：页面不会自动开发、测试或检查远端仓库。</p><button onclick="location.reload()">刷新页面</button><button style="margin-left:8px" onclick="window.print()">打印 / 保存 PDF</button><p><input id="search" aria-label="查找条目" placeholder="搜索阶段或源码核查条目…"></p>
<div class="notice">当前：__CURRENT__<br>不显示主观完成百分比；“模拟验证”“测试通过”“线上验收”分别记录。测试缺失、版本过期与待人工验收均不得算通过。</div>
__EFFICIENCY_STAGE4____EFFICIENCY_STAGE3__
__EFFICIENCY_STAGE2__
__EFFICIENCY_STAGE1__
__INTEGRATION_STAGE3__
__PREPARATION_STAGE2__
__ISOLATION_AUDIT__
__COMPLETED_WORK__
__MATERIALS__
__CONVERSATION__
__STORAGE__
__PERSISTENT_CHAT__
__LEARNING_MEMORY__
__RESOURCE_CONTROLS__
__TASK_MEMORY__
__LIFECYCLE__
<section id="direction"><h2>方向与开发约定</h2><p>__DIRECTION__</p><p>__RECOMMENDATION__</p>__REQUIREMENTS__<h3>决策边界</h3><ul>__DECISIONS__</ul></section>
__ARCHITECTURE__
<section id="stages"><h2>阶段进度与完成条件</h2><div class="cards">__STAGES__</div></section>
<section id="audit"><h2>GitHub 源码核查 · __CHECKED__</h2><p>__SCOPE__</p>__COMMITS__<p>仅浏览固定版本源码。GitHub main 不代表线上部署版本；后续代码改变需重新核查。</p>__FINDINGS__</section>
<section id="tests"><h2>测试证据</h2>__TESTS__<h3>历史模型样例 · 2026-10-28</h3><p>Qwen3.5:9b 三题：课程目标、资料缺失端口、没有工具不能检查。Codex 逐条复核符合当时题目预期，尚未由用户人工验收。耗时约 78.13 / 17.95 / 119.93 秒；仅为小样本，不能作为跨模型或跨环境保证。原始结果在 sandbox/results/20261028-222858-course.json。</p><p>自动检查覆盖基础清理、MCP发现及协议、执行前授权、预算、超时、证据绑定和结果展示。具体数量与版本以最新测试输出为准。模型验收单独记录在顶部进度和已完成事项；自动检查不证明任意回答语义正确。多人、真实Windows/Linux Gateway和远端取消尚未验收。</p></section>
<section id="deployment"><h2>AIVirTeach 部署代价 · 源码评估</h2><p>本节仅评估 AIVirTeach 全套部署：未安装其依赖、启动其服务或构建课程镜像。DSH 开发环境已完成的安装与验证见顶部。资源估算不能替代负载实测。</p>__DEPLOYMENT__</section><section id="risks"><h2>未解决事项与风险</h2><ul>__RISKS__</ul></section>
<section id="use"><h2>如何持续更新</h2><p>development/status.json 是进度、约定和member_report汇报资料的唯一编辑入口；github-review.json 保存固定源码核查；latest-tests.json 由测试命令生成。每次开发结束需更新状态、记录验证并生成页面。</p><pre>python tools/dashboard.py test
python tools/dashboard.py build
python tools/dashboard.py serve</pre><p>build 生成可离线打开的 dashboard/index.html；serve 启动仅监听 127.0.0.1 的只读看板，页面每 5 秒检查本地状态文件或代码指纹的变化并刷新。它不会自行运行模型、远程查 GitHub 或推断功能已完成。服务停止后使用离线 HTML。</p><p>代码/配置/测试指纹变化会让旧基础测试标为待重跑。语义验收和业务状态仍需要有证据的更新。本进度HTML仍是开发快照；实际阶段、分段队列与时延已接独立只读Agent管理面板，二者用途不同。历史图表按原日期和版本保留，最新架构见“项目怎么运行”。</p></section>
</div></main><script>__REPORT_SCRIPT__const exportedSnapshot=false;const revision=__REVISION__;const live=document.getElementById('live');if(!exportedSnapshot&&location.protocol.startsWith('http')){live.textContent='本机只读看板 · 每5秒检查数据变化；当前不是任务运行监控。';setInterval(async()=>{try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();live.textContent='本机只读看板 · 最近检查：'+new Date().toLocaleTimeString();if(d.revision!==revision)location.reload();}catch{live.textContent='本地更新服务未连接；当前页面为已载入快照。';}},5000);}document.getElementById('search').addEventListener('input',e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('.searchable').forEach(el=>el.hidden=!el.textContent.toLowerCase().includes(q));});</script></body></html>'''
    replacements = {"__EFFICIENCY_STAGE4__":render_runtime_optimization(s.get("efficiency_stage4")),"__EFFICIENCY_STAGE3__":render_completion(s.get("efficiency_stage3")),"__EFFICIENCY_STAGE2__":render_low_cost(s.get("efficiency_stage2")),"__EFFICIENCY_STAGE1__":render_efficiency(s.get("efficiency_stage1")), "__INTEGRATION_STAGE3__":render_integration(s.get('integration_stage3')), "__PREPARATION_STAGE2__":render_preparation(s.get("preparation_stage2")), '__REPORT_CSS__':REPORT_CSS, '__REPORT_SCRIPT__':REPORT_SCRIPT, '__TEAM_REPORT__':render_report(s.get('member_report'),t,data['fingerprint']), '__LIFECYCLE__':render_lifecycle(s.get('lifecycle')), '__TASK_MEMORY__':render_task_memory(s.get('task_memory')), '__RESOURCE_CONTROLS__':render_resource_controls(s.get('resources_step4')), '__LEARNING_MEMORY__':render_learning_memory(s.get('learning_memory_step3')), '__PERSISTENT_CHAT__':render_chat_session(s.get('chat_session_step2')), '__STORAGE__':render_storage(s.get('storage_step1')), '__ARCHITECTURE__':render_architecture(s.get('architecture')), '__CONVERSATION__':conversation_html, '__MATERIALS__':material_html, '__ISOLATION_AUDIT__':isolation_html, '__COMPLETED_WORK__':completed_html, '__UPDATED__':esc(s['updated_at']), '__NOW__':esc(now), '__CURRENT__':esc(s['current']), '__DIRECTION__':esc(s['direction']), '__RECOMMENDATION__':esc(s['recommendation']), '__REQUIREMENTS__':requirement_table,'__DECISIONS__':decisions,'__STAGES__':stages,'__CHECKED__':esc(a['checked_at']),'__SCOPE__':esc(a['scope']),'__COMMITS__':commits,'__FINDINGS__':findings,'__TESTS__':tests_html,'__DEPLOYMENT__':table(['项目','源码可确认','边界与待确认'],s.get('deployment',[])),'__RISKS__':risks,'__REVISION__':json.dumps(data['revision'])}
    for key,value in replacements.items():
        page = page.replace(key,value)
    return page


def run_tests():
    before = fingerprint()
    try:
        result = subprocess.run([sys.executable,'-m','unittest','discover','-s','sandbox/tests','-v'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env=__import__('os').environ | {'PYTHONUTF8':'1'},timeout=90)
        exit_code, output = result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        exit_code, output = 124, '基础测试运行超过90秒，未完成；不能视为通过。'
    record = {'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),'exit_code':exit_code,'source_fingerprint':before,'output':output}
    # 同目录原子替换，避免本地看板读到写入一半的报告。
    pending = DEV/'latest-tests.pending'
    pending.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    pending.replace(DEV/'latest-tests.json')
    OUT.write_text(render(load_data()),encoding='utf-8')
    print(record['output'])
    return exit_code


class Handler(BaseHTTPRequestHandler):
    """仅提供固定页面和脱敏状态；不暴露项目目录或执行命令。"""
    def do_GET(self):
        if self.path not in ('/','/index.html','/api/status'):
            self.send_error(404)
            return
        data = load_data()
        is_api = self.path == '/api/status'
        body = (json.dumps({'revision':data['revision']}) if is_api else render(data)).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type','application/json; charset=utf-8' if is_api else 'text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='本地开发看板')
    parser.add_argument('action',choices=['build','test','serve'])
    parser.add_argument('--port',type=int,default=8768)
    args=parser.parse_args()
    if args.action=='test':
        sys.exit(run_tests())
    elif args.action=='build':
        OUT.write_text(render(load_data()),encoding='utf-8')
        print(OUT)
    else:
        print(f'看板：http://127.0.0.1:{args.port}；关闭进程即可停止。',flush=True)
        ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
