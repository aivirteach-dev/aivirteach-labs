"""把虚拟机画面接进诊断流程：画面描述加进评估提示词、回答末尾的说明。

- 有画面时，看图结果作为“画面描述”加进评估（和收尾）的用户消息；使用规则（platform_assessment.SCREEN_RULES）
  一直在系统消息里，所以系统消息不等看图结果，DSH 可以和看图同时启动。模型调用次数、工具和事件都与不带画面时相同。
- 画面描述不是工具证据，没有观察编号；模型仍把它写进 evidence_ids 时在校验前去掉，
  校验本身（platform_assessment.validate，照抄 main）不变。
- 回答末尾的说明：带了画面但没用上时说明原因。
"""
from __future__ import annotations

import json
from typing import Optional

from .platform_assessment import SECURITY_NOTE, parse_output_object
from .platform_screen import (SCREEN_ANIMATED, SCREEN_DIMENSIONS_MISMATCH, SCREEN_DIMENSIONS_TOO_LARGE,
                              SCREEN_FORMAT_MISMATCH, SCREEN_INVALID_BASE64, SCREEN_NOT_ANALYZED, SCREEN_TOO_LARGE,
                              SCREEN_UNREADABLE)
from .platform_vision import (SCREEN_ANALYSIS_FAILED, SCREEN_ANALYSIS_INVALID, SCREEN_ANALYSIS_TIMEOUT,
                              SCREEN_RATE_LIMITED, SCREEN_SENSITIVE_CONTENT, Perception)

SCREEN_EVIDENCE_ID = "screen-1"


_NOTES = {
    "zh": {
        "sensitive": "（你发来的画面上可能有密钥或密码，我没有使用这张截图。请先关闭显示密钥的窗口，再重新发送画面。）",
        "rate_limited": "（查看画面的次数暂时用完了，以上回答没有参考你的画面，请稍后再试。）",
        "unused": "（这次没能查看你发来的虚拟机画面，以上回答没有参考画面。）",
    },
    "en": {
        "sensitive": "(The screen you sent may show a key or password, so I did not use it. Close that window and "
                     "send the screen again.)",
        "rate_limited": "(The screen-view limit has been reached for now, so this answer does not use your screen. "
                        "Please try again later.)",
        "unused": "(I couldn't use the VM screen you sent this time, so this answer does not rely on it.)",
    },
}
_UNUSED_CODES = {SCREEN_INVALID_BASE64, SCREEN_TOO_LARGE, SCREEN_FORMAT_MISMATCH, SCREEN_DIMENSIONS_TOO_LARGE,
                 SCREEN_DIMENSIONS_MISMATCH, SCREEN_ANIMATED, SCREEN_UNREADABLE, SCREEN_NOT_ANALYZED,
                 SCREEN_ANALYSIS_TIMEOUT, SCREEN_ANALYSIS_FAILED, SCREEN_ANALYSIS_INVALID}


def position_words(x: float, y: float, width: float, height: float, language: str) -> str:
    """把比例坐标的中心点换成粗略的位置描述，方便模型用文字告诉学生。"""
    cx, cy = x + width / 2, y + height / 2
    row = 0 if cy < 1 / 3 else 1 if cy < 2 / 3 else 2
    col = 0 if cx < 1 / 3 else 1 if cx < 2 / 3 else 2
    if language.lower().startswith("zh"):
        names = [["左上", "顶部中间", "右上"], ["左侧中间", "正中间", "右侧中间"], ["左下", "底部中间", "右下"]]
    else:
        names = [["top left", "top centre", "top right"], ["middle left", "centre", "middle right"],
                 ["bottom left", "bottom centre", "bottom right"]]
    return names[row][col]


def screen_observation(perception: Perception, language: str) -> dict:
    return {
        "window": perception.window,
        "app": perception.app,
        "page": perception.page,
        "summary": perception.summary,
        "visible_errors": perception.visible_errors,
        "elements": [{"label": e.label, "position": position_words(e.x, e.y, e.width, e.height, language)}
                     for e in perception.elements],
        "readable": perception.readable,
    }


def with_screen(prompts: tuple, perception: Perception, language: str) -> tuple:
    """在评估（或收尾）的用户消息末尾加画面描述；系统消息不变。"""
    system, user = prompts
    block = json.dumps({"screen_observation": screen_observation(perception, language),
                        "security_note": SECURITY_NOTE}, ensure_ascii=False)
    return system, user + "\n\n" + block


def drop_screen_reference(text: Optional[str]) -> Optional[str]:
    """评估 JSON 的 evidence_ids 里出现画面编号时去掉，其余原样交给 main 的校验。解析不了就不动。"""
    if not text:
        return text
    try:
        payload = parse_output_object(text)
    except ValueError:
        return text
    ids = payload.get("evidence_ids")
    if not isinstance(ids, list) or not any(item in (SCREEN_EVIDENCE_ID, "screen") for item in ids):
        return text
    payload["evidence_ids"] = [item for item in ids if item not in (SCREEN_EVIDENCE_ID, "screen")]
    return json.dumps(payload, ensure_ascii=False)


def answer_note(*, screen_attached: bool, screen_used: bool, screen_codes: list, language: str) -> str:
    """回答末尾的说明（没有就返回空字符串）。"""
    notes = _NOTES["zh" if language.lower().startswith("zh") else "en"]
    if screen_attached and not screen_used:
        if SCREEN_SENSITIVE_CONTENT in screen_codes:
            return notes["sensitive"]
        if SCREEN_RATE_LIMITED in screen_codes:
            return notes["rate_limited"]
        if any(code in _UNUSED_CODES for code in screen_codes):
            return notes["unused"]
    return ""
