from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
LAB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
COURSE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
CHECKPOINT_ID_RE = re.compile(r"^P(?:0[1-9]|1[0-9]|2[0-4])$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
EVIDENCE_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProgressTarget(StrictModel):
    target_id: str = Field(min_length=1, max_length=256)
    target_revision: int = Field(ge=1, le=2_147_483_647)
    lab_id: str = Field(min_length=1, max_length=63)
    vm_instance_id: str = Field(min_length=36, max_length=36)
    course_id: str = Field(min_length=1, max_length=128)
    runtime_course_id: str = Field(min_length=1, max_length=128)
    course_version: int = Field(ge=1, le=1_000_000)
    checkpoints: list[str] = Field(min_length=1, max_length=24)

    @field_validator("target_id")
    @classmethod
    def validate_target_id(cls, value: str) -> str:
        if not SAFE_ID_RE.fullmatch(value):
            raise ValueError("invalid target_id")
        return value

    @field_validator("lab_id")
    @classmethod
    def validate_lab_id(cls, value: str) -> str:
        if not LAB_ID_RE.fullmatch(value):
            raise ValueError("invalid lab_id")
        return value

    @field_validator("vm_instance_id")
    @classmethod
    def validate_instance_id(cls, value: str) -> str:
        if not UUID_RE.fullmatch(value):
            raise ValueError("invalid vm_instance_id")
        return value.lower()

    @field_validator("course_id", "runtime_course_id")
    @classmethod
    def validate_course_id(cls, value: str) -> str:
        if not COURSE_ID_RE.fullmatch(value):
            raise ValueError("invalid course ID")
        return value

    @field_validator("checkpoints")
    @classmethod
    def validate_checkpoints(cls, value: list[str]) -> list[str]:
        if any(not CHECKPOINT_ID_RE.fullmatch(item) for item in value):
            raise ValueError("invalid checkpoint ID")
        if len(set(value)) != len(value):
            raise ValueError("duplicate checkpoint ID")
        return value


class TargetsResponse(StrictModel):
    schema_version: Literal[1]
    targets: list[ProgressTarget] = Field(max_length=500)

    @field_validator("targets")
    @classmethod
    def validate_unique_targets(cls, value: list[ProgressTarget]) -> list[ProgressTarget]:
        identities = [(item.target_id, item.target_revision, item.vm_instance_id) for item in value]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate progress target")
        labs = [(item.lab_id, item.vm_instance_id) for item in value]
        if len(set(labs)) != len(labs):
            raise ValueError("duplicate VM progress target")
        return value


ProgressState = Literal["passed", "failed", "unknown"]


class GatewayObservation(StrictModel):
    schema_version: Literal[1]
    course_id: str = Field(min_length=1, max_length=128)
    checkpoint_id: str = Field(min_length=3, max_length=3)
    state: ProgressState
    evidence_type: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=512)
    facts: dict[str, Any]

    @field_validator("course_id")
    @classmethod
    def validate_course_id(cls, value: str) -> str:
        if not COURSE_ID_RE.fullmatch(value):
            raise ValueError("invalid course_id")
        return value

    @field_validator("checkpoint_id")
    @classmethod
    def validate_checkpoint_id(cls, value: str) -> str:
        if not CHECKPOINT_ID_RE.fullmatch(value):
            raise ValueError("invalid checkpoint_id")
        return value

    @field_validator("evidence_type")
    @classmethod
    def validate_evidence_type(cls, value: str) -> str:
        if not EVIDENCE_TYPE_RE.fullmatch(value):
            raise ValueError("invalid evidence_type")
        return value

    @field_validator("facts")
    @classmethod
    def validate_facts(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 4_096:
            raise ValueError("facts are too large")
        return value


class GatewayProgressData(StrictModel):
    schema_version: Literal[1]
    course_id: str
    observations: list[GatewayObservation] = Field(min_length=1, max_length=24)


class GatewayResponse(StrictModel):
    tool: Literal["check_course_progress"]
    lab_id: str
    vm_instance_id: str = Field(min_length=36, max_length=36)
    ok: Literal[True]
    observed_at: datetime
    summary: str
    data: GatewayProgressData
    truncated: Literal[False]
    redaction_count: int = Field(ge=0)
    warnings: list[str] = Field(max_length=8)

    @field_validator("vm_instance_id")
    @classmethod
    def validate_instance_id(cls, value: str) -> str:
        if not UUID_RE.fullmatch(value):
            raise ValueError("invalid vm_instance_id")
        return value.lower()

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(UTC)


class ProgressEvent(StrictModel):
    event_id: str = Field(min_length=36, max_length=36)
    worker_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=192)
    target_revision: int = Field(ge=1)
    lab_id: str = Field(min_length=1, max_length=63)
    vm_instance_id: str = Field(min_length=36, max_length=36)
    course_id: str = Field(min_length=1, max_length=128)
    runtime_course_id: str = Field(min_length=1, max_length=128)
    course_version: int = Field(ge=1)
    checkpoint_id: str = Field(min_length=3, max_length=3)
    sequence: int = Field(ge=1)
    state: ProgressState
    observed_at: datetime
    evidence_type: str = Field(min_length=1, max_length=64)
    evidence_summary: str = Field(min_length=1, max_length=512)
    facts: dict[str, Any]
    checker_schema_version: Literal[1]


class ObservationBatch(StrictModel):
    schema_version: Literal[1]
    worker_id: str = Field(min_length=1, max_length=128)
    events: list[ProgressEvent] = Field(min_length=1, max_length=100)


class ObservationAck(StrictModel):
    accepted_event_ids: list[str] = Field(max_length=100)
