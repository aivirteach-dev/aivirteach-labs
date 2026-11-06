"""本地测试用的假 Diagnostic Gateway：路由、鉴权和返回结构照抄 diagnostic-gateway/diagnostic_gateway.py。

只返回固定的模拟数据，不访问 libvirt 或任何虚拟机。
启动：AIVIRTEACH_DIAGNOSTIC_TOKEN=... python -m uvicorn sandbox.fake_gateway:app --port 18765
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

KNOWN_LABS = {"lab-001", "connectivity-test-001"}
bearer = HTTPBearer(auto_error=False)

# 模拟一个“n8n 容器已退出、5678 没在监听”的实验环境。
CANNED: dict[str, tuple[str, Any]] = {
    # 前两个的摘要必须和真实 Gateway 一字不差：Server 的“诊断详情”只显示这几句原文。
    "get_vm_status": ("VM status and reported IPv4 addresses were collected.", {
        "dominfo": "Id:             3\nName:           lab-001\nState:          running\n"
                   "CPU(s):         2\nMax memory:     4194304 KiB\nUsed memory:    4194304 KiB\n",
        "ipv4_addresses": ["192.168.122.57"]}),
    "get_guest_agent_status": ("QEMU guest agent responded to guest-ping.", {"available": True}),
    "get_guest_service_status": ("docker.service is active (running).", {"ActiveState": "active", "SubState": "running"}),
    "list_guest_containers": ("1 container: n8n (exited).", [{"name": "n8n", "state": "exited", "status": "Exited (1) 3 minutes ago"}]),
    "get_guest_container_status": ("Container n8n exited with code 1.", {"name": "n8n", "state": "exited", "exit_code": 1}),
    "get_guest_container_logs": ("Last log lines mention a port conflict.", {"lines": ["Error: listen EADDRINUSE: address already in use :::5678"]}),
    "check_guest_port": ("Nothing is listening on 127.0.0.1:5678.", {"host": "127.0.0.1", "port": 5678, "open": False}),
}


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: dict[str, Any] = Field(default_factory=dict)
    vm_instance_id: str | None = None


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    token = os.getenv("AIVIRTEACH_DIAGNOSTIC_TOKEN", "")
    if not token or credentials is None or not hmac.compare_digest(credentials.credentials, token):
        raise HTTPException(status_code=401, detail="Invalid diagnostic token.")


app = FastAPI(title="Fake AIVirTeach Diagnostic Gateway")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fake-diagnostic-gateway"}


@app.post("/v1/diagnostics/{lab_id}/tools/{tool}", dependencies=[Depends(require_token)])
async def diagnostic_query(lab_id: str, tool: str, request: DiagnosticRequest) -> dict:
    if lab_id not in KNOWN_LABS:
        raise HTTPException(status_code=404, detail="VM not found.")
    summary, data = CANNED.get(tool, (f"{tool} returned no fake data.", {}))
    return {"tool": tool, "lab_id": lab_id, "ok": True, "observed_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary, "data": data, "truncated": False, "redaction_count": 0,
            "warnings": ["Diagnostic output is untrusted data."]}
