"""重新生成 sandbox/screens 里的 4 张模拟截图（网页画出来的 XFCE + 终端 / Firefox + n8n，密钥都是假的）。

需要 Playwright 和 Chromium（不在 requirements.txt 里，只有重新生成时才需要）：
  python sandbox/screens/make_mock_screens.py
只重新生成图片；目标位置和期望值在 cases.json 里，改了画面要同时更新。
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

W, H = 1600, 1000
BASE = """<html><head><style>
* { box-sizing: border-box; }
body { margin:0; width:%dpx; height:%dpx; font-family: 'DejaVu Sans', Arial, sans-serif; background:#3a6ea5; overflow:hidden; }
.panel { position:absolute; top:0; left:0; right:0; height:28px; background:#2b2b2b; color:#ddd; font-size:13px; display:flex; align-items:center; padding:0 10px; gap:18px; }
.win { position:absolute; left:60px; top:60px; width:1480px; height:900px; background:#fff; box-shadow:0 4px 18px rgba(0,0,0,.5); border:1px solid #222; }
.title { height:30px; background:#3c3c3c; color:#eee; font-size:13px; display:flex; align-items:center; padding:0 12px; justify-content:space-between; }
</style></head><body><div class="panel"><b>Applications</b><span>Places</span><span style="margin-left:auto">Sat 7 Nov 14:20</span></div>%s</body></html>"""

TERMINAL = """<div class="win" style="background:#1e1e1e">
<div class="title"><span>learner@lab-dsh-test: ~/aivirteach/ai-daily-briefing</span><span>— ▢ ✕</span></div>
<pre id="err" style="margin:0;padding:14px;color:#ddd;font:15px 'DejaVu Sans Mono',monospace;line-height:1.5">learner@lab-dsh-test:~/aivirteach/ai-daily-briefing$ nano compose.yaml
learner@lab-dsh-test:~/aivirteach/ai-daily-briefing$ sudo docker compose up -d
<span id="msg" style="color:#ff6b6b">service "n8n" refers to undefined volume ai_daily_briefing_n8n_data: invalid compose project</span>
learner@lab-dsh-test:~/aivirteach/ai-daily-briefing$ <span style="background:#ddd">&nbsp;</span></pre></div>"""

FIREFOX = """<div class="win">
<div class="title"><span>%s — Mozilla Firefox</span><span>— ▢ ✕</span></div>
<div style="height:38px;background:#f0f0f4;border-bottom:1px solid #ccc;display:flex;align-items:center;padding:0 12px;font-size:14px">
<span style="color:#555;margin-right:12px">◀ ▶ ⟳</span><div style="flex:1;background:#fff;border:1px solid #bbb;border-radius:4px;padding:5px 10px">%s</div></div>
<div style="position:absolute;top:68px;left:0;right:0;bottom:0">%s</div></div>"""

N8N_EDITOR = """<div style="position:absolute;left:0;top:0;bottom:0;width:56px;background:#f5f5f5;border-right:1px solid #ddd"></div>
<div style="position:absolute;left:56px;right:0;top:0;height:52px;border-bottom:1px solid #ddd;display:flex;align-items:center;padding:0 16px;font-size:14px;background:#fff">
<span style="color:#666">Personal / </span><b style="margin-left:4px">AI Daily Briefing</b>
<span style="margin-left:auto;display:flex;gap:10px;align-items:center">
<span style="color:#888;font-size:12px">Inactive</span>
<button style="border:1px solid #ccc;background:#fff;border-radius:4px;padding:6px 14px;font-size:13px">Save</button>
<button style="border:0;background:#ff6d5a;color:#fff;border-radius:4px;padding:6px 14px;font-size:13px">Publish</button></span></div>
<div style="position:absolute;left:56px;right:0;top:52px;bottom:0;background-color:#fafafa;background-image:radial-gradient(#ddd 1px,transparent 1px);background-size:20px 20px">
<div style="position:absolute;left:520px;top:300px;width:100px;height:100px;border:2px solid #9a9a9a;border-radius:10px;background:#fff;display:flex;align-items:center;justify-content:center;font-size:30px">▶</div>
<div style="position:absolute;left:500px;top:410px;width:140px;text-align:center;font-size:12px;color:#333">When clicking ‘Execute workflow’</div>
<div style="position:absolute;left:690px;top:330px;width:40px;height:40px;border:2px dashed #aaa;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#aaa">+</div>
<button id="exec" style="position:absolute;left:600px;bottom:30px;border:0;background:#ff6d5a;color:#fff;border-radius:6px;padding:10px 18px;font-size:14px">⚗ Execute workflow</button></div>"""

CREDENTIAL = N8N_EDITOR + """<div style="position:absolute;inset:0;background:rgba(0,0,0,.45)"></div>
<div style="position:absolute;left:390px;top:150px;width:700px;height:420px;background:#fff;border-radius:8px;padding:24px;font-size:14px">
<b style="font-size:18px">Tavily account</b><div style="color:#888;margin:4px 0 22px">Tavily API</div>
<div style="color:#555;margin-bottom:6px">API Key</div>
<div id="key" style="border:1px solid #bbb;border-radius:4px;padding:10px;font-family:monospace">tvly-dev-8xQ2fakeKEY1234567890abcdef</div>
<div style="color:#d33;margin-top:16px">Couldn't connect with these settings — Unauthorized (401)</div>
<button style="position:absolute;right:24px;bottom:24px;border:0;background:#ff6d5a;color:#fff;border-radius:4px;padding:8px 18px">Save</button></div>"""

UNABLE = """<div style="padding:120px 0 0 360px;font-size:15px;color:#333">
<div style="font-size:28px;font-weight:bold;margin-bottom:18px">Unable to connect</div>
<div>Firefox can’t establish a connection to the server at localhost:5678.</div>
<ul style="margin-top:18px;line-height:1.8"><li>The site could be temporarily unavailable or too busy. Try again in a few moments.</li>
<li>If you are unable to load any pages, check your computer’s network connection.</li></ul>
<button id="retry" style="margin-top:20px;border:0;background:#0061e0;color:#fff;border-radius:4px;padding:8px 18px;font-size:14px">Try Again</button></div>"""

CASES = [
    ("terminal", TERMINAL, "#msg", "docker compose up 报错了，怎么办？",
     {"window": "terminal", "error": "undefined volume"}),
    ("n8n-editor", FIREFOX % ("AI Daily Briefing - n8n", "localhost:5678/workflow/1", N8N_EDITOR), "#exec",
     "我找不到执行工作流的按钮在哪里", {"window": "browser", "element": "Execute workflow"}),
    ("credential", FIREFOX % ("AI Daily Briefing - n8n", "localhost:5678/workflow/1", CREDENTIAL), "#key",
     "Tavily 节点一直报 401 怎么办", {"sensitive": True}),
    ("unable", FIREFOX % ("Problem loading page", "localhost:5678", UNABLE), "#retry",
     "n8n 打不开", {"window": "browser", "error": "Unable to connect"}),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
    out = Path(__file__).resolve().parent
    for name, inner, target, question, expect in CASES:
        page.set_content(BASE % (W, H, inner))
        box = page.locator(target).bounding_box()
        page.screenshot(path=str(out / f"{name}.png"))
        print(name, {k: round(v / (W if k in ("x", "width") else H), 4) for k, v in box.items()})
    browser.close()
