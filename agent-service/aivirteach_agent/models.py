from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


LAB_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"
RELATIVE_PATH_MAX = 512


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolName(str, Enum):
    GET_VM_STATUS = "get_vm_status"
    GET_GUEST_AGENT_STATUS = "get_guest_agent_status"
    GET_GUEST_JOURNAL = "get_guest_journal"
    GET_GUEST_SERVICE_STATUS = "get_guest_service_status"
    GET_GUEST_NETWORK_SUMMARY = "get_guest_network_summary"
    GET_GUEST_LISTENING_PORTS = "get_guest_listening_ports"
    CHECK_GUEST_PORT = "check_guest_port"
    CHECK_GUEST_DNS = "check_guest_dns"
    LIST_COURSE_FILES = "list_course_files"
    STAT_COURSE_FILE = "stat_course_file"
    READ_COURSE_FILE = "read_course_file"
    TAIL_COURSE_FILE = "tail_course_file"
    LIST_GUEST_CONTAINERS = "list_guest_containers"
    GET_GUEST_CONTAINER_STATUS = "get_guest_container_status"
    GET_GUEST_CONTAINER_LOGS = "get_guest_container_logs"
    GET_GUEST_CONTAINER_PORTS = "get_guest_container_ports"
    GET_RUNTIME_VERSIONS = "get_runtime_versions"
    GET_RUNTIME_PROCESSES = "get_runtime_processes"
    INSPECT_PYTHON_PROJECT = "inspect_python_project"
    INSPECT_NODE_PROJECT = "inspect_node_project"


class CourseExcerpt(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4_000)


class CourseContext(StrictModel):
    course_id: str = Field(min_length=1, max_length=128)
    version: str | int = Field(default="unknown")
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2_000)
    relevant_excerpts: list[CourseExcerpt] = Field(default_factory=list, max_length=5)


class CommonFailure(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    symptoms: list[str] = Field(default_factory=list, max_length=8)
    likely_causes: list[str] = Field(default_factory=list, max_length=8)
    learner_guidance: str = Field(default="", max_length=2_000)


class LessonContext(StrictModel):
    module_id: str = Field(min_length=1, max_length=128)
    lesson_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1, le=10_000)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2_000)
    instructions: list[str] = Field(default_factory=list, max_length=20)
    expected_result: str = Field(default="", max_length=4_000)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    common_failures: list[CommonFailure] = Field(default_factory=list, max_length=12)


class HistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)


class DiagnosticScope(StrictModel):
    workspace_root: str = Field(default="/home/learner/course", max_length=512)
    allowed_tools: set[ToolName] = Field(
        default_factory=lambda: {
            ToolName.GET_VM_STATUS,
            ToolName.GET_GUEST_AGENT_STATUS,
        },
        max_length=20,
    )
    allowed_relative_paths: list[str] = Field(default_factory=list, max_length=64)
    allowed_services: list[str] = Field(default_factory=list, max_length=32)
    allowed_containers: list[str] = Field(default_factory=list, max_length=32)
    allowed_ports: list[int] = Field(default_factory=list, max_length=32)
    allowed_external_hosts: list[str] = Field(default_factory=list, max_length=32)
    allowed_runtimes: list[Literal["python", "node"]] = Field(
        default_factory=list, max_length=2
    )

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value or "/../" in f"/{value}/":
            raise ValueError("workspace_root must be a normalized absolute guest path")
        return value.rstrip("/") or "/"

    @field_validator("allowed_relative_paths")
    @classmethod
    def validate_relative_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_relative_path(value)
        return values

    @field_validator("allowed_ports")
    @classmethod
    def validate_ports(cls, values: list[int]) -> list[int]:
        if any(port < 1 or port > 65_535 for port in values):
            raise ValueError("allowed_ports values must be between 1 and 65535")
        return values


class DiagnoseRequest(StrictModel):
    request_id: UUID
    lab_id: str = Field(pattern=LAB_ID_PATTERN)
    question: str = Field(min_length=1, max_length=4_000)
    response_language: str = Field(default="zh-CN", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
    course: CourseContext
    current_step: LessonContext
    learner_state: dict[str, Any] = Field(default_factory=dict)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=8)
    diagnostic_scope: DiagnosticScope = Field(default_factory=DiagnosticScope)

    @model_validator(mode="after")
    def limit_dynamic_context(self) -> "DiagnoseRequest":
        encoded = json.dumps(self.learner_state, ensure_ascii=False, default=str)
        if len(encoded) > 16_384:
            raise ValueError("learner_state must be at most 16384 serialized characters")
        return self


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Diagnosis(StrictModel):
    summary: str = Field(default="", max_length=4_000)
    probable_causes: list[str] = Field(default_factory=list, max_length=8)
    confidence: Confidence = Confidence.LOW


class CourseAlignment(StrictModel):
    expected: list[str] = Field(default_factory=list, max_length=12)
    observed: list[str] = Field(default_factory=list, max_length=12)


class SuggestedAction(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=2_000)


class DiagnosticAssessment(StrictModel):
    diagnosis: Diagnosis
    course_alignment: CourseAlignment = Field(default_factory=CourseAlignment)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)


class Evidence(StrictModel):
    id: str
    tool: ToolName
    summary: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ToolTrace(StrictModel):
    tool: str
    status: Literal["ok", "error", "denied", "cached"]
    duration_ms: int = Field(ge=0)
    observation_id: str | None = None
    error_code: str | None = None


class Usage(StrictModel):
    """Token usage normalized from the model provider for metering."""

    input_cache_hit_tokens: int = Field(ge=0)
    input_cache_miss_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class AssessmentOutcome(StrictModel):
    assessment: DiagnosticAssessment | None
    evidence: list[Evidence]
    tool_trace: list[ToolTrace]
    limitations: list[str]
    partial: bool
    usage: Usage | None


class DiagnoseResponse(StrictModel):
    request_id: UUID
    status: Literal["completed", "partial"]
    answer: str
    diagnosis: Diagnosis
    course_alignment: CourseAlignment
    evidence: list[Evidence]
    suggested_actions: list[SuggestedAction]
    limitations: list[str]
    tool_trace: list[ToolTrace]
    usage: Usage | None = None


def validate_relative_path(value: str) -> str:
    if not value or len(value) > RELATIVE_PATH_MAX:
        raise ValueError("relative path must contain 1-512 characters")
    if value.startswith("/") or "\x00" in value:
        raise ValueError("path must be relative")
    parts = value.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError("path traversal is not allowed")
    return value


def valid_resource_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@_.:-]{0,127}", value):
        raise ValueError("invalid resource name")
    return value
