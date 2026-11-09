"""整条诊断流程的重复评测：真实对话模型 + 真实看图模型 + 真实 DSH，配假 Gateway 和仓库里的模拟截图。

统计三件事：诊断对不对（假 Gateway 按 lab_id 模拟不同故障，答案里要出现对应的原因和做法）、
快不快（总耗时、首字、评估从第几秒开始）、token 多少。
只连模型，不连 Gateway 和任何 VM；截图是 sandbox/screens/ 里的模拟画面（只有假密钥）。

两种模式对比预先检查的效果：
  prechecks（默认）：和上线时一样，请求不带工具范围，按课程配置确定范围并预先检查。
  model：请求带上同样的工具范围、不做预先检查，由模型自己决定查什么（改之前的做法）。

用法（在 dsh-prototype 目录，先 set -a; . ./.env.platform; set +a 读入模型配置）：
  python -m sandbox.pipeline_eval --runs 5
  python -m sandbox.pipeline_eval --runs 10 --mode model --cases env-stopped,ui-screen --dump-dir /tmp/assessments

每个请求打印一行 JSON（状态、是否答对、原因码、评估失败的类别、每次调用的阶段/开始/耗时/首字/结束原因、token），
最后一行是汇总。--dump-dir：评估没通过校验时，把评估阶段的模型原文写进这个目录（输入都是模拟数据，只用于本地排查）。
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

from dsh_tutor import platform_precheck as precheck
from dsh_tutor.platform_app import Diagnoser, Settings, normalize_usage
from dsh_tutor.platform_assessment import is_answer_stage
from dsh_tutor.platform_course import CourseRepository
from dsh_tutor.platform_runtime import run_once
from dsh_tutor.platform_screen import ScreenDiagnoseRequest
from dsh_tutor.telemetry import Telemetry
from sandbox import fake_gateway, fake_model

SCREENS = Path(__file__).resolve().parent / "screens"
GATEWAY_TOKEN = "pipeline-eval-gateway-token"
ENV_QUESTION = "n8n 打不开，浏览器显示无法连接，怎么办？"
UI_QUESTION = "我找不到执行工作流的按钮在哪里"
INSTALL = {"module_id": "configure-the-runtime-environment", "lesson_id": "install-and-start-n8n", "sequence": 2,
           "title": "Install and Start n8n"}
TRIGGER = {"module_id": "build-the-ai-daily-briefing-workflow", "lesson_id": "trigger", "sequence": 3, "title": "Trigger"}
# expect：每组里至少出现一个词（不分大小写），所有组都要满足，才算答对。检查回答、诊断摘要和可能原因。
# forbid_in_actions：建议里不能出现的词。allow_partial：工具注定失败的环境，结果是 partial 也算数。
CASES = {
    "env-stopped": dict(lab="lab-stopped", question=ENV_QUESTION, step=INSTALL, screen=None,
                        expect=[["停止", "退出", "没有运行", "未运行", "exited", "stopped"],
                                ["up -d", "compose up", "docker start", "compose start"]]),
    "env-conflict": dict(lab="lab-001", question=ENV_QUESTION, step=INSTALL, screen=None,
                         expect=[["EADDRINUSE", "占用", "冲突"]]),
    "env-missing": dict(lab="lab-missing", question="按教程做完后 n8n 还是打不开，哪里出错了？", step=INSTALL, screen=None,
                        expect=[["不存在", "没有创建", "未创建", "还没有创建", "找不到", "No such"],
                                ["up -d", "compose up"]]),
    "ui-screen": dict(lab="lab-healthy", question=UI_QUESTION, step=TRIGGER, screen="n8n-editor.png",
                      expect=[["Execute"]]),
    "ui-text": dict(lab="lab-healthy", question=UI_QUESTION, step=TRIGGER, screen=None, expect=[["Execute"]]),
    # 服务器测试 R2-05：虚拟机已关机、工具都查不到，截图却是旧的（n8n 开着）。不能凭画面说服务正常。
    "stale-screen": dict(lab="lab-down", question=UI_QUESTION, step=TRIGGER, screen="n8n-editor.png",
                         allow_partial=True, expect=[["关机", "没有运行", "未运行", "没在运行", "无法确认", "没能确认", "无法访问", "shut off",
                                  "启动虚拟机", "开机", "不可用", "没有响应"]]),
    # 服务器测试 R2-02：匹配不到课程（没有教材和课程配置）时问连接被拒绝，不能建议改成 0.0.0.0。
    "no-textbook": dict(lab="lab-healthy", question="n8n 打不开，浏览器显示连接被拒绝，为什么？", step=INSTALL,
                        screen=None, course_id="no-textbook-check", explicit_scope=True,
                        expect=[["localhost", "127.0.0.1"]], forbid_in_actions=["0.0.0.0"]),
}


def screen_payload(name: str) -> dict:
    raw = (SCREENS / name).read_bytes()
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
    return {"source": "vm_console", "mime_type": "image/png", "data": base64.b64encode(raw).decode(),
            "width": width, "height": height, "captured_at": datetime.now(timezone.utc).isoformat()}


def request_for(case: dict, mode: str, config: precheck.ScopeConfig) -> ScreenDiagnoseRequest:
    data = {"request_id": str(uuid.uuid4()), "lab_id": case["lab"], "question": case["question"],
            "course": {"course_id": case.get("course_id", "ai-daily-briefing"), "version": 1, "title": "AI Daily Briefing"},
            "current_step": case["step"]}
    if mode == "model" or case.get("explicit_scope"):  # model：改之前的做法，同样的工具范围由请求带来，不做预先检查
        data["diagnostic_scope"] = config.courses[0].scope.model_dump(mode="json")
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


def unsafe(case: dict, response) -> bool:
    actions = " ".join(f"{a.title} {a.detail}" for a in response.suggested_actions).casefold()
    return any(word.casefold() in actions for word in case.get("forbid_in_actions", []))


def correct(case: dict, response) -> bool:
    text = " ".join([response.answer, response.diagnosis.summary, *response.diagnosis.probable_causes]).casefold()
    return bool((response.status == "completed" or case.get("allow_partial"))
                and all(any(word.casefold() in text for word in group) for group in case["expect"])
                and not unsafe(case, response))


async def one(diagnoser: Diagnoser, recording: Recording, name: str, case: dict, dump_dir, mode: str,
              config: precheck.ScopeConfig) -> dict:
    request = request_for(case, mode, config)
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
    assessing = [c for c in calls if c["phase"] == "assessment"]
    if dump_dir and not correct(case, response):  # 没答对时留下回答和建议（输入都是模拟数据），方便人工看原因
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
        (Path(dump_dir) / f"{name}-{str(request.request_id)[:8]}-answer.json").write_text(json.dumps(
            {"answer": response.answer, "diagnosis": response.diagnosis.model_dump(mode="json"),
             "suggested_actions": [a.model_dump(mode="json") for a in response.suggested_actions]},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return {"case": name, "status": response.status, "correct": correct(case, response),
            "unsafe_advice": unsafe(case, response), "total_s": round(total, 1),
            "first_text_s": round(marks["first_text"], 1) if "first_text" in marks else None,
            "assessment_starts_s": assessing[0]["at_s"] if assessing else None,
            "assessment_calls": len(assessing),
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
            "correct": f"{sum(r['correct'] for r in group)}/{len(group)}",
            "avg_assessment_calls": round(statistics.mean(r["assessment_calls"] for r in group), 1),
            "avg_tools": round(statistics.mean(r["tools"] for r in group), 1),
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
            "avg_cache_miss_tokens": round(statistics.mean(r["usage"]["input_cache_miss_tokens"] for r in group
                                                           if r["usage"])) if tokens else None,
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
        config = precheck.load_config()
        diagnoser = Diagnoser(settings, recording, CourseRepository(settings.course_directory), Telemetry(),
                              scopes=config if args.mode == "prechecks" else precheck.ScopeConfig())
        for run in range(args.runs):
            for name in names:
                case = dict(CASES[name], question=CASES[name]["question"] + ("" if run == 0 else f"（第 {run + 1} 次）"))
                row = await one(diagnoser, recording, name, case, args.dump_dir, args.mode, config)
                row["run"] = run + 1
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps({"summary": summarise(rows), "mode": args.mode, "model": base.model_name,
                      "vision_model": base.vision_model or None}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the full diagnosis repeatedly against the real model.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cases", default=",".join(CASES))
    parser.add_argument("--mode", choices=("prechecks", "model"), default="prechecks")
    parser.add_argument("--dump-dir", default=None)
    args = parser.parse_args(argv)
    unknown = set(args.cases.split(",")) - set(CASES)
    if unknown:
        parser.error(f"unknown cases: {sorted(unknown)}")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
