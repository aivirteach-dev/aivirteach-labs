from target_architecture import render_target
"""成员汇报页渲染：只消费进度数据，生成可离线放大、打印的文字连线图。

与业务代码分离；不连接数据库、模型或平台。文字统一转义，图形只接受已知
节点类型、有限坐标及本地维护的数据，不加载 CDN，也不执行教材中的内容。
"""
import html
import unicodedata


def esc(value):
    return html.escape(str(value), quote=True)


def diagram(graph):
    """文字图保留分支与汇合；固定中文占两格，防止中英混排使连线错位。"""
    pieces=[]
    for line in graph['text'].splitlines():
        chars=[]
        for char in line:
            if unicodedata.east_asian_width(char) in 'WF':
                chars.append('<span class="wide-cell">'+esc(char)+'</span>')
            else:chars.append(esc(char))
        pieces.append(''.join(chars))
    return '<pre class="text-flow" tabindex="0" aria-label="'+esc(graph['title'])+'">'+'\n'.join(pieces)+'</pre>'


def render_report(report, tests, fingerprint):
    """先展示用途和流程；技术资料收起，状态仍来自权威进度数据。"""
    if not report:return ''
    out=['<div id="team-report" class="report-view" hidden><header class="report-hero"><span class="badge">给项目成员看的架构说明</span><h1>学生各有记录，<br>助教和计算资源怎样共享？</h1><p class="report-lead">'+esc(report['summary'])+'</p></header>']
    out.append('<div class="plain-status">'+''.join('<article><strong>'+esc(a)+'</strong><p>'+esc(b)+'</p></article>' for a,b in report['plain_status'])+'</div>')
    out.append('<nav class="report-nav" aria-label="架构目录">'+''.join('<a href="#'+esc(g['id'])+'">'+esc(g['title'])+'</a>' for g in report['diagrams'])+'<a href="#report-target">目标架构与开发约定</a></nav>')
    out.append(render_target(report.get('target_architecture'), diagram))
    for g in report['diagrams']:
        out+=['<section class="report-section" id="'+esc(g['id'])+'"><h2>'+esc(g['title'])+'</h2><p class="section-intro">'+esc(g['intro'])+'</p><figure><div class="diagram-scroll">',diagram(g),'</div><figcaption>'+esc(g['caption'])+'</figcaption></figure><button type="button" class="diagram-zoom" data-diagram="'+esc(g['id'])+'">放大这张图</button>']
        for title,body in g['details']:out.append('<h3>'+esc(title)+'</h3><p>'+esc(body)+'</p>')
        out.append('</section>')
    out.append('<section id="report-delivery"><h2>现在能确认到什么程度？</h2><p>自动检查已验证程序的一部分规则；模拟演示没有检查真实电脑，也不能证明模型每次都答对。服务器还没有完成多人容量测试。</p></section>')
    out.append('<section id="report-sources"><details class="report-detail"><summary>开发成员：技术、文件和验证依据</summary>')
    for group in report['tables']:
        out.append('<h3>'+esc(group['title'])+'</h3><div class="table-wrap"><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in group['headers'])+'</tr></thead><tbody>')
        out.extend('<tr>'+''.join('<td>'+esc(c)+'</td>' for c in row)+'</tr>' for row in group['rows']);out.append('</tbody></table></div>')
    out.append('<p>'+esc(report['source_note'])+'</p><ul>')
    for source in report['sources']:
        url=source.get('url','');label=esc(source['title'])
        if url and not url.startswith('https://github.com/aivirteach-dev/'):raise ValueError('invalid_source')
        out.append('<li>'+('<a target="_blank" rel="noopener" href="'+esc(url)+'">'+label+'</a>' if url else label)+'：'+esc(source['note'])+'</li>')
    matched=tests and tests.get('source_fingerprint')==fingerprint and tests.get('exit_code')==0
    out.append('</ul><p>最近自动检查：'+('源码指纹一致，通过' if matched else '需重新核对')+'；记录时间：'+esc(tests.get('at','无记录') if tests else '无记录')+'</p></details></section></div><dialog id="diagram-dialog" aria-label="放大的流程图"><button type="button" id="diagram-close">关闭（Esc）</button><div id="diagram-large"></div></dialog>')
    return ''.join(out)


REPORT_CSS='''
.view-switch,.report-nav{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 24px}.view-switch a,.report-nav a{padding:8px 14px;background:white;border:1px solid #d0ded8;border-radius:8px;text-decoration:none}.view-switch a[aria-current="page"]{background:#176b5c;color:white}.report-hero{background:white;border-top:5px solid #176b5c;border-radius:12px;padding:28px}.report-hero h1{line-height:1.4;font-size:34px}.report-lead{font-size:18px;max-width:850px}.plain-status{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:20px 0}.plain-status article{background:white;border:1px solid #d3dfd9;border-radius:10px;padding:18px}.plain-status strong{font-size:18px;color:#176b5c}.plain-status p{font-size:14px}.section-intro{font-size:18px}.report-section{scroll-margin-top:20px}.report-section figure{margin:16px 0 8px}.diagram-scroll{overflow:auto;background:#f7faf8;border:1px solid #d3e0d8;border-radius:12px}/* 本页采用文字连线，不加载绘图库。横向滚动只发生在图内。 */
.diagram-scroll{background:#292a29;border-color:#353b36;padding:24px 28px}
.text-flow{font:18px/1.85 Consolas,"Liberation Mono",monospace;color:#f0f3f0;background:transparent;border:0;border-radius:0;white-space:pre;overflow-wrap:normal;margin:0;padding:0;min-width:max-content;letter-spacing:0;font-variant-ligatures:none}
.wide-cell{display:inline-block;width:2ch;text-align:center;font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif}
figcaption{font-size:14px;color:#526a61;margin-top:10px}.report-detail{border:1px solid #d3dfd9;background:white;border-radius:8px;padding:12px 16px;margin:14px 0}.report-detail summary{cursor:pointer;font-weight:600}.report-view td{overflow-wrap:anywhere}dialog{width:1450px;max-width:98vw;max-height:95vh;background:#f7faf8;border:1px solid #9cb5aa;border-radius:12px;padding:16px}dialog::backdrop{background:#132d35b3}#diagram-large{overflow:auto;margin-top:12px}#diagram-large{background:#292a29;padding:24px}#diagram-large .text-flow{font-size:22px}body.report-active aside .progress-nav{display:none}body:not(.report-active) aside .report-sidebar{display:none}@media(max-width:850px){.plain-status{grid-template-columns:1fr}.report-hero{padding:18px}.report-hero h1{font-size:27px}}@media print{.view-switch,.report-nav,.report-detail,.diagram-zoom,dialog{display:none!important}.diagram-scroll{overflow:visible;border:0}.diagram-scroll{background:white;border:1px solid #ddd}.text-flow{color:#18392d;font-size:10px}.report-section figure{break-inside:avoid}.report-hero{padding:0}.report-section h2{break-after:avoid}}
'''


REPORT_SCRIPT='''
// hash 同时记录页签和章节；转发带 #team-report 的文件即可直接汇报。
const reportView=document.getElementById('team-report');
const progressView=document.getElementById('progress-view');
function selectReportView(){
  const target=location.hash.slice(1);
  const active=!target||target==='team-report'||target.startsWith('report-');
  reportView.hidden=!active;progressView.hidden=active;
  document.body.classList.toggle('report-active',active);
  document.querySelectorAll('[data-view]').forEach(a=>{
    if(a.dataset.view===(active?'report':'progress'))a.setAttribute('aria-current','page');
    else a.removeAttribute('aria-current');
  });
  // 页签刚解除 hidden 后，浏览器需要重新定位内部章节。
  const anchor=document.getElementById(target);
  if(anchor&&!anchor.hidden)requestAnimationFrame(()=>anchor.scrollIntoView());
}
window.addEventListener('hashchange',selectReportView);selectReportView();
const diagramDialog=document.getElementById('diagram-dialog');
document.querySelectorAll('.diagram-zoom').forEach(button=>button.addEventListener('click',()=>{
  const copy=document.querySelector('#'+button.dataset.diagram+' pre').cloneNode(true);
  document.getElementById('diagram-large').replaceChildren(copy);diagramDialog.showModal();
}));
document.getElementById('diagram-close').addEventListener('click',()=>diagramDialog.close());
diagramDialog.addEventListener('close',()=>document.getElementById('diagram-large').replaceChildren());
'''
