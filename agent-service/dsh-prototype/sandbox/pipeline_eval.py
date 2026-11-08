"""整条诊断流程的重复评测：真实对话模型 + 真实看图模型 + 真实 DSH，配假 Gateway 和仓库里的模拟截图。

用来查带截图的诊断为什么不稳定（服务器测试里出现过超时和评估没通过校验），并统计耗时和 token。
只连模型，不连 Gateway 和任何 VM；截图是 sandbox/screens/ 里的模拟画面（只有假密钥）。

用法（在 dsh-prototype 目录，先 set -a; . ./.env.platform; set +a 读入模型配置）：
  python -m sandbox.pipeline_eval --runs 5
  python -m sandbox.pipeline_eval --runs 10 --cases s-ui,s-env --dump-dir /tmp/assessments

每个请求打印一行 JSON（状态、原因码、评估失败的类别、每次调用的阶段/耗时/首字/结束原因、token），最后一行是汇总。
--dump-dir：评估没通过校验时，把评估阶段的模型原文写进这个目录（输入都是模拟数据，只用于本地排查）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import io
import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from dsh_tutor.platform_app import Diagnoser, Settings, normalize_usage
from dsh_tutor.platform_assessment import is_answer_stage
from dsh_tutor.platform_course import CourseRepository
from dsh_tutor.platform_runtime import run_once
from dsh_tutor.platform_screen import ScreenDiagnoseRequest
from dsh_tutor.telemetry import Telemetry
from sandbox import fake_gateway, fake_model

SCREENS = Path(__file__).resolve().parent / "screens"
GATEWAY_TOKEN = "pipeline-eval-gateway-token"
SCOPE = {"allowed_tools": ["get_vm_status", "get_guest_agent_status", "list_guest_containers",
                           "get_guest_container_status", "get_guest_container_logs", "check_guest_port"],
         # 教材里的容器名是 ai-daily-briefing-n8n，假 Gateway 的固定数据里叫 n8n，两个都允许，避免模型照教材查时被拒绝
         "allowed_containers": ["ai-daily-briefing-n8n", "n8n"], "allowed_ports": [5678]}
CASES = {
    "plain": dict(question="n8n 打不开，浏览器显示连接被拒绝，怎么办？", screen=None, scope=True),
    "s-ui": dict(question="我找不到执行工作流的按钮在哪里", screen="n8n-editor.png", scope=False),
    "s-env": dict(question="n8n 打不开，浏览器显示无法连接，怎么办？", screen="unable.png", scope=True),
    "s-broken": dict(question="我找不到执行工作流的按钮在哪里", screen="broken", scope=False),
}


def screen_payload(name: str) -> dict:
    if name == "broken":  # 文件头像 PNG、内容是乱码：应得到 SCREEN_UNREADABLE，诊断照常完成
        raw = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR\x00\x00\x06@\x00\x00\x03\xe8\x08\x02\x00\x00\x00" + os.urandom(4096)
        width, height = 1600, 1000
    else:
        raw = (SCREENS / name).read_bytes()
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    return {"source": "vm_console", "mime_type": "image/png", "data": base64.b64encode(raw).decode(),
            "width": width, "height": height, "captured_at": datetime.now(timezone.utc).isoformat()}


def request_for(case: dict) -> ScreenDiagnoseRequest:
    data = {"request_id": str(uuid.uuid4()), "lab_id": "lab-001", "question": case["question"],
            "course": {"course_id": "ai-daily-briefing", "version": 1, "title": "AI Daily Briefing"},
            "current_step": {"module_id": "module-1", "lesson_id": "install-n8n", "sequence": 2,
                             "title": "安装并启动 n8n"}}
    if case["scope"]:
        data["diagnostic_scope"] = SCOPE
    if case["screen"]:
        data["screen"] = screen_payload(case["screen"])
    return ScreenDiagnoseRequest.model_validate(data)


class Recording:
    """包住 run_once：记下评估阶段的原文，评估没通过校验时（--dump-dir）写到本地供排查。"""

    def __init__(self):
        self.assessment_text = None

    def __call__(self, prompt, dsh, on_event, cancel=None, tools=None):
        outcome = run_once(prompt, dsh, on_event, cancel, tools)
        if not is_answer_stage(dsh.system_prompt):
            self.assessment_text = outcome.answer
        return outcome


async def one(diagnoser: Diagnoser, recording: Recording, name: str, case: dict, dump_dir) -> dict:
    request = request_for(case)
    recording.assessment_text = None
    started, started_ms = time.monotonic(), time.time() * 1000
    marks: dict = {}

    async def publish(event: str, data: dict) -> None:
        if event == "assistant_delta" and "first_text" not in marks:
            marks["first_text"] = time.monotonic() - started

    outcomes = []
    original = diagnoser._pipeline

    def pipeline(*args):  # 取到各次调用的记录（只有阶段、时间和结束原因）
        result = original(*args)
        outcomes.append(result)
        return result

    diagnoser._pipeline = pipeline
    try:
        response = await diagnoser.diagnose(request, publish)
    finally:
        diagnoser._pipeline = original
    total = time.monotonic() - started
    result = outcomes[0] if outcomes else None
    usage = normalize_usage(result.usage) if result else None
    error = result.assessment_error if result else ""
    if error and dump_dir and recording.assessment_text is not None:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
        (Path(dump_dir) / f"{name}-{str(request.request_id)[:8]}.txt").write_text(recording.assessment_text, encoding="utf-8")
    calls = []
    for call in (result.calls if result else []):
        calls.append({"phase": call.phase, "at_s": round((call.start_ms - started_ms) / 1000, 1),
                      "s": round((call.end_ms - call.start_ms) / 1000, 1) if call.end_ms else None,
                      "first_s": round((call.first_delta_ms - call.start_ms) / 1000, 1) if call.first_delta_ms else None,
                      "finish": call.finish or call.error, "retries": getattr(call, "retries", 0)})
    codes = [c for c in response.limitations if c.isupper() and " " not in c]
    return {"case": name, "status": response.status, "total_s": round(total, 1),
            "first_text_s": round(marks["first_text"], 1) if "first_text" in marks else None,
            "codes": codes, "assessment_error": error or None,
            "tools": sum(1 for e in (result.tool_events if result else []) if e.get("name") == "tool_started"),
            "denied": [f"{t.get('tool')}:{t.get('error_code')}" for t in (result.tool_trace if result else [])
                       if t.get("status") == "denied"],
            "dropped": result.dropped_fields if result else [],
            "calls": calls, "usage": usage.model_dump() if usage else None,
            "answer_chars": len(response.answer), "highlights": len(response.screen_findings.highlights)
            if getattr(response, "screen_findings", None) else 0}


def summarise(rows: list) -> dict:
    out = {}
    for name in sorted({r["case"] for r in rows}):
        group = [r for r in rows if r["case"] == name]
        done = [r for r in group if r["status"] == "completed"]
        tokens = [sum(r["usage"].values()) for r in group if r["usage"]]
        out[name] = {
            "completed": f"{len(done)}/{len(group)}",
            "avg_total_s": round(statistics.mean(r["total_s"] for r in group), 1),
            "max_total_s": max(r["total_s"] for r in group),
            "avg_first_text_s": round(statistics.mean(r["first_text_s"] for r in done if r["first_text_s"]), 1)
            if any(r["first_text_s"] for r in done) else None,
            "avg_tokens": round(statistics.mean(tokens)) if tokens else None,
            "avg_cache_hit": round(statistics.mean(r["usage"]["input_cache_hit_tokens"] /
                                                   max(1, r["usage"]["input_cache_hit_tokens"] +
                                                       r["usage"]["input_cache_miss_tokens"]) for r in group
                                                   if r["usage"]), 2) if tokens else None,
            "avg_output_tokens": round(statistics.mean(r["usage"]["output_tokens"] for r in group if r["usage"]))
            if tokens else None,
            "avg_model_calls": round(statistics.mean(len(r["calls"]) for r in group), 1),
            "codes": dict(Counter(c for r in group for c in r["codes"])),
            "assessment_errors": dict(Counter(r["assessment_error"] for r in group if r["assessment_error"])),
            "retries": sum(c["retries"] for r in group for c in r["calls"]),
        }
    return out


async def main_async(args) -> int:
    base = Settings.from_env()
    if base.model_provider != "openai_compatible" or not base.model_api_key_set:
        print("set AIVIRTEACH_MODEL_* (provider openai_compatible) first, e.g. from .env.platform", file=sys.stderr)
        return 2
    os.environ["AIVIRTEACH_DIAGNOSTIC_TOKEN"] = GATEWAY_TOKEN  # 假 Gateway 读这个令牌
    names = args.cases.split(",")
    rows = []
    with fake_model.serve(fake_gateway.app) as gateway_url, tempfile.TemporaryDirectory() as state:
        settings = dataclasses.replace(base, gateway_url=gateway_url, diagnostic_token=GATEWAY_TOKEN,
                                       state_dir=Path(state), screen_max_per_hour=0)
        recording = Recording()
        diagnoser = Diagnoser(settings, recording, CourseRepository(settings.course_directory), Telemetry())
        for run in range(args.runs):
            for name in names:
                case = dict(CASES[name], question=CASES[name]["question"] + ("" if run == 0 else f"（第 {run + 1} 次）"))
                row = await one(diagnoser, recording, name, case, args.dump_dir)
                row["run"] = run + 1
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps({"summary": summarise(rows), "model": base.model_name,
                      "vision_model": base.vision_model or None}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the full diagnosis repeatedly against the real model.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cases", default="plain,s-ui,s-env,s-broken")
    parser.add_argument("--dump-dir", default=None)
    args = parser.parse_args(argv)
    unknown = set(args.cases.split(",")) - set(CASES)
    if unknown:
        parser.error(f"unknown cases: {sorted(unknown)}")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
