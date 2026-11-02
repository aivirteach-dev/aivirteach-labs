"""渲染目标说明；内容唯一来源是status.json，不改业务代码。"""
import html

def render_target(record, diagram):
    """目标区内容由同一JSON记录生成；沿用既有文字图渲染器。"""
    if not record:return ''
    e=lambda t:html.escape(str(t),quote=True)
    out=['<section id="report-target"><h2>已确认的目标架构与开发约定</h2><div class="notice"><strong>'+e(record['state'])+'</strong><br>'+e(record['purpose'])+'</div><p>确认日期：'+e(record['recorded_on'])+'。各阶段状态与验收依据分别记录；计划内容不能当作已实现。</p>',
         '<figure><div class="diagram-scroll">'+diagram({'title':'目标架构：请求调度、记忆与资源分配','text':record['flow']})+'</div></figure><p>'+e(record['baseline'])+'</p><h3>服务器测试前，按三轮推进</h3>']
    for phase in record['phases']:
        out.append('<article class="report-detail"><strong>'+e(phase['title'])+'</strong> <span class="badge">'+e(phase['state'])+'</span><p>'+e(phase['work'])+'</p><p><b>完成条件：</b>'+e(phase['acceptance'])+'</p></article>')
    out.append('<details class="report-detail"><summary>必须遵守的开发规则</summary><ul>')
    for title,body in record['rules']:out.append('<li><b>'+e(title)+'：</b>'+e(body)+'</li>')
    out.append('</ul></details><details class="report-detail"><summary>要测哪些指标？哪些必须等服务器？</summary><h3>应采集的指标</h3><ul>')
    out.extend('<li>'+e(v)+'</li>' for v in record['measure']);out.append('</ul><h3>实际服务器上确认</h3><ul>')
    out.extend('<li>'+e(v)+'</li>' for v in record['server_only']);out.append('</ul></details><details class="report-detail"><summary>设计参考与版本边界</summary><p>参考公开文档的设计思路；不是已安装或已验证能力。本机DSH保持0.2.1rc2，未来采用主线能力前须验证兼容。</p><ul>')
    out.extend('<li><a target="_blank" rel="noopener" href="'+e(url)+'">'+e(title)+'</a></li>' for title,url in record['references']);out.append('</ul></details></section>')
    return ''.join(out)
