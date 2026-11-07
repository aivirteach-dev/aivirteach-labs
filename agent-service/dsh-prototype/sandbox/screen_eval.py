"""看图评测：用真实看图模型逐张检查截图，对照 cases.json 里的期望值打分。只连看图模型，不连 Gateway / VM。

用法（在 dsh-prototype 目录）：
  python -m sandbox.screen_eval sandbox/screens/cases.json --runs 3
  python -m sandbox.screen_eval ~/dsh-test/screens/cases.json --runs 2 --model deepseek-flash

模型地址默认 https://api.deepseek.com/v1，密钥读 AIVIRTEACH_VISION_MODEL_API_KEY，没有就读 AIVIRTEACH_MODEL_API_KEY；
不打印密钥。每次看图打印一行 JSON，最后一行是汇总。

cases.json：{"cases": [{"name", "image"（相对 cases.json 的路径）, "question", "expect": {...}}]}，expect 可以有：
  window            期望的窗口类型（terminal / browser / editor / file_manager / dialog / desktop / other）
  error_contains    屏幕报错里应包含的文字（不区分大小写，空格和下划线视为相同）
  element_contains  相关元素的标签里应包含的文字
  target            目标元素的真实位置（0～1 比例）；元素框要盖住目标中心才算命中
  region            没有 target 时，用粗略位置判断（如 "左上"、"底部中间"，见 platform_screen_flow.position_words）
  sensitive         true 表示画面有密钥，应被拒用（SCREEN_SENSITIVE_CONTENT）
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from dsh_tutor.platform_screen import ScreenCapture, load_screen
from dsh_tutor.platform_screen_flow import position_words
from dsh_tutor.platform_vision import SCREEN_SENSITIVE_CONTENT, ScreenAnalyzer, VisionSettings

MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _loose(text: str) -> str:
    return re.sub(r"[\s_]+", " ", text).strip().lower()


def score(expect: dict, result) -> dict:
    """返回每项检查是否通过（只包含 expect 里给出的项）。"""
    checks = {}
    perception = result.perception
    if expect.get("sensitive"):
        checks["sensitive"] = result.code == SCREEN_SENSITIVE_CONTENT
        return checks
    checks["usable"] = result.status == "ok" and perception is not None
    if perception is None:
        return {**checks, **{key: False for key in ("window", "error", "element") if _wanted(expect, key)}}
    if "window" in expect:
        checks["window"] = perception.window == expect["window"]
    if "error_contains" in expect:
        wanted = _loose(expect["error_contains"])
        checks["error"] = any(wanted in _loose(item) for item in perception.visible_errors)
    if "element_contains" in expect:
        wanted = _loose(expect["element_contains"])
        candidates = [e for e in perception.elements if wanted in _loose(e.label)]
        if "target" in expect:
            t = expect["target"]
            cx, cy = t["x"] + t["width"] / 2, t["y"] + t["height"] / 2
            checks["element"] = any(e.x <= cx <= e.x + e.width and e.y <= cy <= e.y + e.height for e in candidates)
        elif "region" in expect:
            checks["element"] = any(position_words(e.x, e.y, e.width, e.height, "zh-CN") == expect["region"]
                                    for e in candidates)
        else:
            checks["element"] = bool(candidates)
    return checks


def _wanted(expect: dict, key: str) -> bool:
    return {"window": "window", "error": "error_contains", "element": "element_contains"}[key] in expect


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the vision step on labelled screenshots.")
    parser.add_argument("cases", type=Path)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--model", default=os.getenv("AIVIRTEACH_VISION_MODEL_NAME") or "deepseek-flash")
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    args = parser.parse_args(argv)

    key_env = next((name for name in ("AIVIRTEACH_VISION_MODEL_API_KEY", "AIVIRTEACH_MODEL_API_KEY")
                    if os.getenv(name)), None)
    if key_env is None:
        print("no API key in AIVIRTEACH_VISION_MODEL_API_KEY or AIVIRTEACH_MODEL_API_KEY", file=sys.stderr)
        return 2
    # max_per_hour=0：评测不受每台 VM 的次数限制；每次换一个问题后缀，避免复用上一次的结果。
    tool = ScreenAnalyzer(VisionSettings(model=args.model, base_url=args.base_url.rstrip("/"), api_key_env=key_env,
                                         timeout_seconds=30, max_per_hour=0))
    spec = json.loads(args.cases.read_text(encoding="utf-8"))
    totals: dict = {}
    seconds, tokens = [], []
    for run in range(args.runs):
        for case in spec["cases"]:
            raw = (args.cases.parent / case["image"]).read_bytes()
            with Image.open(io.BytesIO(raw)) as image:
                size, mime = image.size, MIME.get(image.format or "", "image/png")
            capture = ScreenCapture(mime_type=mime, data=base64.b64encode(raw).decode(), width=size[0],
                                    height=size[1], captured_at=datetime.now(timezone.utc))
            started = time.monotonic()
            result = tool.analyze(load_screen(capture), question=case["question"], lesson_title=f"eval run {run + 1}",
                                  lab_id="screen-eval", language="zh-CN", budget_seconds=60)
            elapsed = round(time.monotonic() - started, 1)
            checks = score(case.get("expect", {}), result)
            for name, passed in checks.items():
                ok, total = totals.get(name, (0, 0))
                totals[name] = (ok + int(passed), total + 1)
            used = sum(u["inputTokens"] + u["cacheReadTokens"] + u["outputTokens"] for u in result.usage)
            seconds.append(elapsed)
            tokens.append(used)
            p = result.perception
            print(json.dumps({"run": run + 1, "case": case["name"], "status": result.status, "code": result.code,
                              "passes": result.passes, "seconds": elapsed, "tokens": used, "checks": checks,
                              "window": p.window if p else None, "summary": p.summary if p else None,
                              "errors": p.visible_errors if p else None,
                              "elements": [{"label": e.label, "box": [e.x, e.y, e.width, e.height]}
                                           for e in (p.elements if p else [])]}, ensure_ascii=False))
    summary = {name: f"{ok}/{total}" for name, (ok, total) in totals.items()}
    summary.update(avg_seconds=round(statistics.mean(seconds), 1) if seconds else None,
                   avg_tokens=round(statistics.mean(tokens)) if tokens else None, model=args.model)
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
