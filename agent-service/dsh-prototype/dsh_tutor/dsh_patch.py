"""告诉 DSH 这次请求怎么运行：模型提供方、预算插件、工具桥插件，渲染成 DSH 补丁文件。

DSH 自带的文件、Shell、摘要、重试等插件在基础补丁（BASE_PATCH）里关闭；这里只追加本请求需要的插件。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .platform_mcp import SERVER_NAME

ROOT = Path(__file__).resolve().parent.parent
BASE_PATCH = ROOT / "dsh-base.patch.yml"
BUDGET_PLUGIN = ROOT / "dsh_tutor" / "plugins" / "model_budget.mjs"
# 关闭思考时发给提供方的值；DeepSeek 和 Ollama 实测都接受。
REASONING_OFF_VALUE = "none"
OLLAMA_KEY_ENV = "OLLAMA_PLACEHOLDER_KEY"  # Ollama 不需要密钥，但 DSH 要求提供一个变量名


@dataclass(frozen=True)
class Provider:
    """DSH 里注册的模型提供方。api_key_env 为 None 表示 Ollama。"""

    id: str
    base_url: str
    api_key_env: Optional[str] = None

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password \
                or url.path != "/v1" or url.query or url.fragment:
            raise ValueError("provider base_url must be http(s)://host[:port]/v1 without credentials")
        if self.api_key_env is not None and (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", self.api_key_env)
                                             or self.api_key_env.startswith(("OLLAMA_PLACEHOLDER", "DSH_"))):
            raise ValueError("provider api_key_env must be an upper-case name outside OLLAMA_PLACEHOLDER*/DSH_*")

    def environment(self) -> dict:
        """密钥只从宿主环境读取，不写进补丁文件。"""
        if self.api_key_env is None:
            return {OLLAMA_KEY_ENV: "local-ollama"}
        value = os.environ.get(self.api_key_env, "")
        if not value or len(value) > 8192 or any(c in value for c in "\r\n"):
            raise ValueError("provider_auth_unavailable")
        return {self.api_key_env: value}


def provider_plugin(provider: Provider, *, model: str, context_window: int, max_output_tokens: int,
                    reasoning_off: bool) -> dict:
    """用 DSH 的 pi-ai 适配注册一个 OpenAI 兼容提供方；关闭思考时给出提供方接受的值。"""
    entry = {"id": model, "name": model, "api": "openai-completions", "contextWindow": context_window,
             "maxTokens": max_output_tokens, "compat": {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}}
    if reasoning_off:
        entry["reasoningEfforts"] = {"off": REASONING_OFF_VALUE, "low": "low"}
    return {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {provider.id: {
        "displayName": provider.id, "apiKeyEnv": provider.api_key_env or OLLAMA_KEY_ENV, "api": "openai-completions",
        "baseURL": provider.base_url, "defaultContextWindow": context_window, "defaultMaxTokens": max_output_tokens,
        "retryPolicy": {"mode": "normal", "maxRetries": 0}, "models": [entry]}}}}


# 单次模型调用多久还没输出任何内容就放弃并重试一次（服务器测试里出现过一次调用卡住、用光 40 秒总时限）。
# 实测正常请求的首字在 1～4 秒；评估阶段的提示词最长约 2 万 token，留足余量。
FIRST_TOKEN_SECONDS = 15


def budget_plugin(*, provider_id: str, model: str, max_model_calls: int, max_output_tokens: int,
                  max_output_bytes: int, deadline_unix: float, allowed_tools: list, run_dir: Path,
                  first_token_seconds: int = FIRST_TOKEN_SECONDS) -> dict:
    """预算插件只读这里给的值；模型不能改变路径、工具集或上限。"""
    return {"id": "tutor-model-budget", "name": str(BUDGET_PLUGIN), "config": {
        "provider": provider_id, "model": model, "maxModelCalls": max_model_calls, "maxTokens": max_output_tokens,
        "maxOutputBytes": max_output_bytes, "deadlineUnix": deadline_unix, "allowedTools": allowed_tools,
        "firstTokenMs": int(first_token_seconds * 1000),
        "cancelPath": str(run_dir / "cancel.request"), "budgetPath": str(run_dir / "budget.json"),
        "streamPath": str(run_dir / "stream.jsonl")}}


def tool_plugin(context_path: Path, timeout_seconds: int) -> dict:
    """DSH 官方 MCP 客户端插件：stdio 启动本请求独占的工具桥，不开放网络端口。"""
    return {"id": "mcp-course-tools", "name": "@deepseek-ai/dsh-mcp-client", "config": {
        "serverName": SERVER_NAME, "transport": "stdio", "command": sys.executable,
        "args": ["-m", "dsh_tutor.platform_mcp", str(context_path)], "cwd": str(ROOT),
        "env": {"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        "toolCallTimeoutMs": (timeout_seconds + 2) * 1000, "failOnStartupError": True, "reconnect": {"enabled": False}}}


def render_patch(plugins: list) -> str:
    return BASE_PATCH.read_text(encoding="utf-8-sig") + "\n- insert: " + json.dumps(plugins, ensure_ascii=False) + "\n"


def read_budget_stop(run_dir: Path) -> Optional[str]:
    """插件写入的停止原因；文件不存在说明插件没有挂载，按失败处理。"""
    path = run_dir / "budget.json"
    if not path.is_file() or path.stat().st_size > 1024:
        raise RuntimeError("budget_plugin_not_mounted")
    value = json.loads(path.read_text(encoding="utf-8")).get("budget_stop")
    return value if isinstance(value, str) else None

