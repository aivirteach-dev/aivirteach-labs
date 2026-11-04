# 工具参数与授权校验：复制自 agent-service/aivirteach_agent/tools.py。
# 只删去 main 专用的模型工具定义（ProviderTool）和网关调用函数，校验规则一字未改。
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .platform_models import DiagnosticScope, ToolName, valid_resource_name, validate_relative_path


class ToolPolicyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


class JournalArgs(ToolArgs):
    service: str
    lines: int = Field(default=100, ge=1, le=200)
    since_minutes: int = Field(default=60, ge=1, le=1_440)

    _service = field_validator("service")(valid_resource_name)


class ServiceArgs(ToolArgs):
    service: str

    _service = field_validator("service")(valid_resource_name)


class DnsArgs(ToolArgs):
    hostname: str = Field(min_length=1, max_length=253)

    @field_validator("hostname")
    @classmethod
    def hostname_is_safe(cls, value: str) -> str:
        if not re.fullmatch(r"(?i)[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value):
            raise ValueError("invalid hostname")
        return value.lower()


class PortArgs(ToolArgs):
    host: str = Field(default="127.0.0.1")
    port: int = Field(ge=1, le=65_535)


class PathArgs(ToolArgs):
    path: str = Field(default=".", max_length=512)

    _path = field_validator("path")(validate_relative_path)


class ReadFileArgs(PathArgs):
    max_bytes: int = Field(default=16_384, ge=1, le=32_768)


class TailFileArgs(PathArgs):
    lines: int = Field(default=100, ge=1, le=200)


class ContainerArgs(ToolArgs):
    container: str

    _container = field_validator("container")(valid_resource_name)


class ContainerLogsArgs(ContainerArgs):
    lines: int = Field(default=100, ge=1, le=200)
    since_minutes: int = Field(default=60, ge=1, le=1_440)


class RuntimeArgs(ToolArgs):
    runtime: str = Field(pattern=r"^(python|node)$")


class ProjectArgs(PathArgs):
    runtime: str = Field(pattern=r"^(python|node)$")


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    args_model: type[ToolArgs]

    def input_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


TOOL_SPECS: dict[ToolName, ToolSpec] = {
    ToolName.GET_VM_STATUS: ToolSpec(ToolName.GET_VM_STATUS, "Read VM power and resource status.", NoArgs),
    ToolName.GET_GUEST_AGENT_STATUS: ToolSpec(ToolName.GET_GUEST_AGENT_STATUS, "Check whether the QEMU guest agent responds.", NoArgs),
    ToolName.GET_GUEST_JOURNAL: ToolSpec(ToolName.GET_GUEST_JOURNAL, "Read bounded journal entries for an allowed systemd service.", JournalArgs),
    ToolName.GET_GUEST_SERVICE_STATUS: ToolSpec(ToolName.GET_GUEST_SERVICE_STATUS, "Read safe status properties for an allowed systemd service.", ServiceArgs),
    ToolName.GET_GUEST_NETWORK_SUMMARY: ToolSpec(ToolName.GET_GUEST_NETWORK_SUMMARY, "Read guest addresses, routes, and resolver state.", NoArgs),
    ToolName.GET_GUEST_LISTENING_PORTS: ToolSpec(ToolName.GET_GUEST_LISTENING_PORTS, "List listening TCP and UDP sockets without process arguments.", NoArgs),
    ToolName.CHECK_GUEST_PORT: ToolSpec(ToolName.CHECK_GUEST_PORT, "Check an allowed loopback TCP port inside the guest.", PortArgs),
    ToolName.CHECK_GUEST_DNS: ToolSpec(ToolName.CHECK_GUEST_DNS, "Resolve an allowed course hostname inside the guest.", DnsArgs),
    ToolName.LIST_COURSE_FILES: ToolSpec(ToolName.LIST_COURSE_FILES, "List bounded non-sensitive files below an allowed course path.", PathArgs),
    ToolName.STAT_COURSE_FILE: ToolSpec(ToolName.STAT_COURSE_FILE, "Read metadata for an allowed course file.", PathArgs),
    ToolName.READ_COURSE_FILE: ToolSpec(ToolName.READ_COURSE_FILE, "Read a bounded non-sensitive text course file.", ReadFileArgs),
    ToolName.TAIL_COURSE_FILE: ToolSpec(ToolName.TAIL_COURSE_FILE, "Read the bounded tail of an allowed text course file.", TailFileArgs),
    ToolName.LIST_GUEST_CONTAINERS: ToolSpec(ToolName.LIST_GUEST_CONTAINERS, "List containers and their high-level states.", NoArgs),
    ToolName.GET_GUEST_CONTAINER_STATUS: ToolSpec(ToolName.GET_GUEST_CONTAINER_STATUS, "Read selected safe status fields for an allowed container.", ContainerArgs),
    ToolName.GET_GUEST_CONTAINER_LOGS: ToolSpec(ToolName.GET_GUEST_CONTAINER_LOGS, "Read bounded logs for an allowed container.", ContainerLogsArgs),
    ToolName.GET_GUEST_CONTAINER_PORTS: ToolSpec(ToolName.GET_GUEST_CONTAINER_PORTS, "Read published ports for an allowed container.", ContainerArgs),
    ToolName.GET_RUNTIME_VERSIONS: ToolSpec(ToolName.GET_RUNTIME_VERSIONS, "Read the installed version of an allowed Python or Node runtime.", RuntimeArgs),
    ToolName.GET_RUNTIME_PROCESSES: ToolSpec(ToolName.GET_RUNTIME_PROCESSES, "List runtime process names without command-line arguments.", RuntimeArgs),
    ToolName.INSPECT_PYTHON_PROJECT: ToolSpec(ToolName.INSPECT_PYTHON_PROJECT, "Inspect allowed Python project manifests without executing project code.", ProjectArgs),
    ToolName.INSPECT_NODE_PROJECT: ToolSpec(ToolName.INSPECT_NODE_PROJECT, "Inspect allowed Node project manifests without executing scripts.", ProjectArgs),
}

FILE_TOOLS = {
    ToolName.LIST_COURSE_FILES,
    ToolName.STAT_COURSE_FILE,
    ToolName.READ_COURSE_FILE,
    ToolName.TAIL_COURSE_FILE,
    ToolName.INSPECT_PYTHON_PROJECT,
    ToolName.INSPECT_NODE_PROJECT,
}
SERVICE_TOOLS = {ToolName.GET_GUEST_JOURNAL, ToolName.GET_GUEST_SERVICE_STATUS}
CONTAINER_TOOLS = {
    ToolName.GET_GUEST_CONTAINER_STATUS,
    ToolName.GET_GUEST_CONTAINER_LOGS,
    ToolName.GET_GUEST_CONTAINER_PORTS,
}
RUNTIME_TOOLS = {
    ToolName.GET_RUNTIME_VERSIONS,
    ToolName.GET_RUNTIME_PROCESSES,
    ToolName.INSPECT_PYTHON_PROJECT,
    ToolName.INSPECT_NODE_PROJECT,
}
SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}


def validate_tool_call(
    name: str,
    raw_arguments: dict[str, Any],
    scope: DiagnosticScope,
) -> tuple[ToolName, dict[str, Any]]:
    try:
        tool_name = ToolName(name)
    except ValueError as exc:
        raise ToolPolicyError("UNKNOWN_TOOL", "The requested tool does not exist.") from exc
    if tool_name not in scope.allowed_tools:
        raise ToolPolicyError("TOOL_NOT_ALLOWED", "The current course step does not allow this tool.")

    try:
        args = TOOL_SPECS[tool_name].args_model.model_validate(raw_arguments)
    except ValidationError as exc:
        raise ToolPolicyError("INVALID_TOOL_ARGUMENTS", str(exc)[:1_000]) from exc
    values = args.model_dump()

    if tool_name in SERVICE_TOOLS and values["service"] not in scope.allowed_services:
        raise ToolPolicyError("SERVICE_NOT_ALLOWED", "The service is outside the course diagnostic scope.")
    if tool_name in CONTAINER_TOOLS and values["container"] not in scope.allowed_containers:
        raise ToolPolicyError("CONTAINER_NOT_ALLOWED", "The container is outside the course diagnostic scope.")
    if tool_name == ToolName.CHECK_GUEST_PORT:
        if values["host"] not in {"127.0.0.1", "::1", "localhost"}:
            raise ToolPolicyError("HOST_NOT_ALLOWED", "Port checks are limited to guest loopback.")
        if values["port"] not in scope.allowed_ports:
            raise ToolPolicyError("PORT_NOT_ALLOWED", "The port is outside the course diagnostic scope.")
    if tool_name == ToolName.CHECK_GUEST_DNS:
        allowed_hosts = {host.lower() for host in scope.allowed_external_hosts}
        if values["hostname"].lower() not in allowed_hosts:
            raise ToolPolicyError("HOST_NOT_ALLOWED", "The hostname is outside the course diagnostic scope.")
    if tool_name in RUNTIME_TOOLS and values["runtime"] not in scope.allowed_runtimes:
        raise ToolPolicyError("RUNTIME_NOT_ALLOWED", "The runtime is outside the course diagnostic scope.")
    if tool_name == ToolName.INSPECT_PYTHON_PROJECT and values["runtime"] != "python":
        raise ToolPolicyError("INVALID_TOOL_ARGUMENTS", "Python inspection requires runtime=python.")
    if tool_name == ToolName.INSPECT_NODE_PROJECT and values["runtime"] != "node":
        raise ToolPolicyError("INVALID_TOOL_ARGUMENTS", "Node inspection requires runtime=node.")
    if tool_name in FILE_TOOLS:
        _validate_scoped_path(values["path"], scope.allowed_relative_paths)
        values["workspace_root"] = scope.workspace_root
    return tool_name, values


def _validate_scoped_path(path: str, allowed_paths: list[str]) -> None:
    normalized = str(PurePosixPath(path))
    parts = {part.lower() for part in PurePosixPath(normalized).parts}
    if parts & SENSITIVE_NAMES or any("private" in part and "key" in part for part in parts):
        raise ToolPolicyError("SENSITIVE_PATH", "Sensitive credential files cannot be inspected.")
    for allowed in allowed_paths:
        allowed_normalized = str(PurePosixPath(allowed))
        if allowed_normalized == "." or normalized == allowed_normalized or normalized.startswith(f"{allowed_normalized.rstrip('/')}/"):
            return
    raise ToolPolicyError("PATH_NOT_ALLOWED", "The path is outside the course diagnostic scope.")


def tool_cache_key(tool: ToolName, arguments: dict[str, Any]) -> str:
    return f"{tool.value}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"

