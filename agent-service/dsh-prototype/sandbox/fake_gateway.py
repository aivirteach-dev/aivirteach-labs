"""本地测试用的假 Diagnostic Gateway：路由、鉴权和返回结构照抄 diagnostic-gateway/diagnostic_gateway.py。

只返回固定的模拟数据，不访问 libvirt 或任何虚拟机。不同的 lab_id 模拟不同的故障（见 LABS），
评测（sandbox/pipeline_eval.py）用它判断诊断是否找对了原因。
在虚拟机里执行命令的工具（容器、端口、日志、服务、文件）和真实 Gateway 一样只给笼统的摘要
（“… completed with exit code 0.”），结果在 data 的 stdout 里，模型要自己读。
启动：AIVIRTEACH_DIAGNOSTIC_TOKEN=... python -m uvicorn sandbox.fake_gateway:app --port 18765
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

bearer = HTTPBearer(auto_error=False)
CONTAINER = "ai-daily-briefing-n8n"

# 前两个的摘要必须和真实 Gateway 一字不差：Server 的“诊断详情”只显示这几句原文。
CANNED: dict[str, tuple[str, Any]] = {
    "get_vm_status": ("VM status and reported IPv4 addresses were collected.", {
        "dominfo": "Id:             3\nName:           lab-001\nState:          running\n"
                   "CPU(s):         2\nMax memory:     4194304 KiB\nUsed memory:    4194304 KiB\n",
        "ipv4_addresses": ["192.168.122.57"]}),
    "get_guest_agent_status": ("QEMU guest agent responded to guest-ping.", {"available": True}),
}

COMPOSE = (f"services:\n  n8n:\n    image: docker.n8n.io/n8nio/n8n:2.31.7\n    container_name: {CONTAINER}\n"
           "    restart: unless-stopped\n    ports:\n      - \"127.0.0.1:5678:5678\"\n"
           "    volumes:\n      - ai_daily_briefing_n8n_data:/home/node/.n8n\n"
           "volumes:\n  ai_daily_briefing_n8n_data:\n    external: true\n")


def _status(status: str, exit_code: int, error: str = "") -> str:
    running = status == "running"
    return json.dumps({"status": status, "running": running, "exit_code": exit_code, "error": error,
                       "started_at": "2026-11-09T01:00:00Z", "finished_at": "0001-01-01T00:00:00Z" if running
                       else "2026-11-09T01:03:00Z", "image": "docker.n8n.io/n8nio/n8n:2.31.7",
                       "ports": {"5678/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5678"}]} if running else {}})


def _port(reachable: bool) -> str:
    if reachable:
        return json.dumps({"reachable": True, "host": "127.0.0.1", "port": 5678, "latency_ms": 1})
    return json.dumps({"reachable": False, "host": "127.0.0.1", "port": 5678, "error": "ConnectionRefusedError"})


def _ps(status: str) -> str:
    return json.dumps({"Names": CONTAINER, "Image": "docker.n8n.io/n8nio/n8n:2.31.7", "State": status,
                       "Status": {"running": "Up 3 minutes", "exited": "Exited (1) 3 minutes ago",
                                  "stopped": "Exited (0) 10 minutes ago"}[status]})


# 每种实验环境里，在虚拟机里执行命令的工具返回什么：(exit_code, stdout, stderr)。
SCENARIOS: dict[str, dict[str, tuple[int, str, str]]] = {
    # n8n 启动时 5678 被别的程序占用，容器退出
    "port_conflict": {
        "get_guest_container_status": (0, _status("exited", 1), ""),
        "get_guest_container_logs": (0, "2026-11-09T01:02:59Z Error: listen EADDRINUSE: address already in use :::5678", ""),
        "check_guest_port": (0, _port(False), ""),
        "list_guest_containers": (0, _ps("exited"), ""),
    },
    # 学生停掉了容器（或重启虚拟机后没再启动），日志只有正常退出
    "stopped": {
        "get_guest_container_status": (0, _status("exited", 0), ""),
        "get_guest_container_logs": (0, "2026-11-09T00:50:00Z Received SIGTERM. Shutting down...\n"
                                        "2026-11-09T00:50:01Z n8n stopped", ""),
        "check_guest_port": (0, _port(False), ""),
        "list_guest_containers": (0, _ps("stopped"), ""),
    },
    # 还没执行 docker compose up：没有这个容器
    "missing": {
        "get_guest_container_status": (1, "", f"Error: No such object: {CONTAINER}"),
        "get_guest_container_logs": (1, "", f"Error response from daemon: No such container: {CONTAINER}"),
        "check_guest_port": (0, _port(False), ""),
        "list_guest_containers": (0, "", ""),
    },
    # 一切正常（界面类问题）
    "healthy": {
        "get_guest_container_status": (0, _status("running", 0), ""),
        "get_guest_container_logs": (0, "2026-11-09T01:00:05Z Editor is now accessible via:\nhttp://localhost:5678", ""),
        "check_guest_port": (0, _port(True), ""),
        "list_guest_containers": (0, _ps("running"), ""),
    },
}
SHARED: dict[str, tuple[int, str, str]] = {
    "get_guest_service_status": (0, "Id=docker.service\nLoadState=loaded\nActiveState=active\nSubState=running", ""),
    "read_course_file": (0, COMPOSE, ""),
    "list_course_files": (0, "compose.yaml", ""),
}
LABS = {"lab-001": "port_conflict", "connectivity-test-001": "port_conflict", "lab-stopped": "stopped",
        "lab-missing": "missing", "lab-healthy": "healthy", "lab-down": "down"}
# 虚拟机关机：和真实 Gateway 一样，虚拟机状态照常返回（State: shut off），guest agent 没有响应，
# 要在虚拟机里执行命令的工具都返回 409（Agent 记为 GUEST_AGENT_UNAVAILABLE）。
DOWN_VM_STATUS = ("VM status and reported IPv4 addresses were collected.", {
    "dominfo": "Id:             -\nName:           lab-down\nState:          shut off\nCPU(s):         2\n", "ipv4_addresses": []})


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: dict[str, Any] = Field(default_factory=dict)
    vm_instance_id: str | None = None


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    token = os.getenv("AIVIRTEACH_DIAGNOSTIC_TOKEN", "")
    if not token or credentials is None or not hmac.compare_digest(credentials.credentials, token):
        raise HTTPException(status_code=401, detail="Invalid diagnostic token.")


app = FastAPI(title="Fake AIVirTeach Diagnostic Gateway")
app.state.calls = []  # (lab_id, tool)：测试用来确认哪些检查真的访问了 Gateway


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "fake-diagnostic-gateway"}


@app.post("/v1/diagnostics/{lab_id}/tools/{tool}", dependencies=[Depends(require_token)])
async def diagnostic_query(lab_id: str, tool: str, request: DiagnosticRequest) -> dict:
    if lab_id not in LABS:
        raise HTTPException(status_code=404, detail="VM not found.")
    app.state.calls.append((lab_id, tool))
    if LABS[lab_id] == "down":
        if tool == "get_vm_status":
            summary, data = DOWN_VM_STATUS
        elif tool == "get_guest_agent_status":
            summary, data = "QEMU guest agent did not respond.", {"available": False}
        else:
            raise HTTPException(status_code=409, detail="QEMU guest agent is not available.")
    elif tool in CANNED:
        summary, data = CANNED[tool]
    else:
        exit_code, stdout, stderr = SCENARIOS[LABS[lab_id]].get(tool) or SHARED.get(tool) or (0, "", "")
        summary = f"{tool} completed with exit code {exit_code}."
        data = {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "truncated": False}
    return {"tool": tool, "lab_id": lab_id, "ok": True, "observed_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary, "data": data, "truncated": False, "redaction_count": 0,
            "warnings": ["Diagnostic output is untrusted data."]}
