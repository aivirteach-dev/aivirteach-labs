"""工具入口：可信绑定、执行前授权、持久尝试预算和可追踪结果。"""
import asyncio
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, ValidationError

TOOL_NAME = 'get_service_status'


class ServiceArgs(BaseModel):
    """模型只提交服务名称，身份和实例不能由模型指定。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    service: str = Field(min_length=1, max_length=64)


class ToolPolicy(BaseModel):
    """由可信调用方绑定的本轮范围；不是学生问题的一部分。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str
    run_id: str
    student_id: str
    instance_id: str
    course_id: str
    course_version: str
    learning_context_id: str | None = Field(default=None, min_length=1, max_length=128)
    lesson_id: str | None = Field(default=None, min_length=1, max_length=128)
    instance_generation: str | None = Field(default=None, min_length=1, max_length=128)
    allowed_tools: list[str]
    allowed_services: list[str]
    allowed_sources: list[Literal['simulation', 'gateway']] = Field(default_factory=lambda: ['simulation'], min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    deadline_unix: float


class Observation(BaseModel):
    """后端不能提供或覆盖授权身份、证据编号等受信任绑定。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    state: Literal['running', 'stopped', 'unknown']
    source: Literal['simulation', 'gateway']
    note: str = Field(max_length=1024)


class AuditLog:
    """跨 MCP 探测/重连进程共用日志，预算不会因重启而归零。"""
    def __init__(self, path: Path):
        self.path = path

    def append(self, event: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a+', encoding='utf-8') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            raw = stream.read(131073)
            if len(raw.encode('utf-8')) > 131072:
                raise RuntimeError('工具审计达到上限，停止调用。')
            if event['type'] == 'attempt':
                rows = [json.loads(line) for line in raw.splitlines() if line]
                event = {**event, 'attempt': sum(x['type'] == 'attempt' for x in rows) + 1}
            event = {**event, 'at': datetime.now(timezone.utc).isoformat()}
            encoded = json.dumps(event, ensure_ascii=False) + '\n'
            if len(encoded.encode('utf-8')) > 8192:
                raise RuntimeError('工具审计事件过长，停止调用。')
            stream.seek(0, 2)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            return event

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x]


class ToolGateway:
    """只执行获准工具；后端通过函数传入，不依赖模拟资料目录。"""
    def __init__(self, policy: ToolPolicy, audit: AuditLog,
                 executor: Callable[[str, ToolPolicy], Awaitable[dict[str, Any]]]):
        self.policy, self.audit, self.executor = policy, audit, executor

    async def call(self, name: str, arguments: dict) -> dict:
        policy = self.policy
        audit_name = str(name)[:128]
        event = self.audit.append({'type': 'attempt', 'tool': audit_name})
        attempt = event['attempt']

        def result(status: str, message: str, evidence=None):
            row = {'attempt': attempt, 'tool': audit_name, 'status': status, 'message': message,
                   'request_id': policy.request_id, 'run_id': policy.run_id,
                   'instance_id': policy.instance_id}
            if evidence is not None:
                row['evidence'] = evidence
            self.audit.append({'type': 'result', **row})
            return row

        # 无效与被拒绝的尝试同样占预算；在任何后端执行之前检查。
        if attempt > policy.max_attempts:
            return result('budget_exhausted', '工具尝试次数已达到本轮上限。')
        if time.time() >= policy.deadline_unix:
            return result('timeout', '本轮检查时限已到。')
        if name != TOOL_NAME or name not in policy.allowed_tools:
            return result('denied', '当前请求未授权此工具。')
        try:
            args = ServiceArgs.model_validate(arguments)
        except ValidationError:
            return result('invalid', '参数不符合工具约定；不能提交身份、路径或其他附加字段。')
        if args.service not in policy.allowed_services:
            return result('denied', '服务不在当前课程和实例的授权范围内。')
        timeout = min(policy.timeout_seconds, policy.deadline_unix - time.time())
        if timeout <= 0:
            return result('timeout', '本轮检查时限已到。')
        self.audit.append({'type': 'execution', 'attempt': attempt, 'tool': name})
        try:
            observed = await asyncio.wait_for(self.executor(args.service, policy), timeout)
            observed = Observation.model_validate(observed).model_dump()
            if observed['source'] not in policy.allowed_sources:
                raise ValueError('后端数据来源不在授权范围内')
            # 约束输出大小与枚举；失败或畸形数据不能变成有效证据。
            if observed.get('state') not in {'running', 'stopped', 'unknown'}:
                raise ValueError('无效状态')
            if len(json.dumps(observed, ensure_ascii=False).encode()) > 4096:
                raise ValueError('输出过长')
        except asyncio.TimeoutError:
            return result('timeout', '工具执行超时，未获得有效证据。')
        except Exception:
            return result('failed', '工具执行失败，未获得有效证据。')
        evidence = {'evidence_id': uuid4().hex, 'request_id': policy.request_id,
                    'run_id': policy.run_id, 'instance_id': policy.instance_id,
                    'course_id': policy.course_id, 'course_version': policy.course_version,
                    'instance_generation': policy.instance_generation,
                    'observed_at': datetime.now(timezone.utc).isoformat(),
                    'service': args.service, **observed}
        return result('ok', '检查已返回观察结果。', evidence)
