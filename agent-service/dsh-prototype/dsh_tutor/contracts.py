"""课程问答接口。只表示当前课时问答，不冒充已完成的 VM 诊断接口。"""
from typing import Literal
from .conversation import ContextUsage
from .execution_budget import RuntimeMetrics
from pydantic import BaseModel, ConfigDict, Field


class MaterialExcerpt(BaseModel):
    """摘录在提取块中的字符范围，用于追溯原文；不是执行授权。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    block_id: str = Field(min_length=1, max_length=32)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class MaterialContext(BaseModel):
    """精确来源与摘录范围；图片未识别和摘录不全必须显式标记。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    source_filename: str = Field(min_length=1, max_length=255)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor: str = Field(min_length=1, max_length=128)
    excerpts: list[MaterialExcerpt] = Field(max_length=12)
    excerpt_only: bool
    images_interpreted: bool = False
    omitted_code_blocks: int = Field(default=0,ge=0)


class CourseSnapshot(BaseModel):
    """由可信调用方提供当前版本资料；课程内容本身不能授予工具权限。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    course_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    lesson_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    locale: str = Field(default="zh-CN", min_length=2, max_length=35, pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    goals: list[str] = Field(default_factory=list, max_length=12)
    material: MaterialContext | None = None
    instructions: list[str] = Field(default_factory=list, max_length=20)


class CourseQuestion(BaseModel):
    """请求 ID 用于追踪，不等同于数据库去重键；身份授权由调用方承担。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    course: CourseSnapshot
    question: str = Field(min_length=1, max_length=2000)


class ServiceEvidence(BaseModel):
    """当前服务检查证据；身份和来源由可信工具入口绑定，非模型输出。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    course_id: str = Field(min_length=1, max_length=128)
    course_version: str = Field(min_length=1, max_length=128)
    instance_generation: str | None = Field(default=None, min_length=1, max_length=128)
    observed_at: str = Field(min_length=1, max_length=64)
    service: str = Field(min_length=1, max_length=64)
    state: Literal["running", "stopped", "unknown"]
    source: Literal["simulation", "gateway"]
    note: str = Field(max_length=1024)


class AnswerQuality(BaseModel):
    """区分可验证事实与受限说明；不表示任意自然语言或课程相关性已被证明。"""
    facts: Literal["not_requested", "verified", "unavailable", "rejected"] = "not_requested"
    explanation: Literal["not_checked", "validated_selection", "omitted"] = "not_checked"
    issues: list[str] = Field(default_factory=list)


class CourseAnswer(BaseModel):
    """包含模拟工具证据及有限质量状态；不代表完整VM诊断或学生过关。"""
    request_id: str
    execution_path: Literal["model","direct"] = "model"
    direct_action_kind: Literal["material_block","learning_goal","service_status"] | None = None
    memory_revision: int | None = Field(default=None,ge=0)
    run_id: str = ""
    context_usage: ContextUsage | None = None
    runtime_metrics: RuntimeMetrics | None = None
    quality: AnswerQuality = Field(default_factory=AnswerQuality)
    status: Literal["completed", "partial"] = "completed"
    answer: str
    model: str
    elapsed_seconds: float
    tool_calls: int = 0
    tool_attempts: int = 0
    tool_trace: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=lambda: ["仅使用传入的课程资料，未检查真实实验环境。"])
