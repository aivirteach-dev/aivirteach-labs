"""校验本轮工具证据并生成事实回答；不信任模型自由改写检查结果。"""
import json
from datetime import datetime, timezone, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .contracts import CourseQuestion, ServiceEvidence
from .mcp_bridge import BridgeSpec
from .tools import ToolPolicy, TOOL_NAME

PRESENTATION = {
    'zh-CN': {
        'states': {'running':'正在运行','stopped':'已停止','unknown':'暂时无法确定'},
        'failures': {
            'denied':'检查未获授权，工具未执行。',
            'invalid':'检查参数不符合要求，工具未执行。',
            'budget_exhausted':'本轮工具尝试次数已达上限。',
            'timeout':'检查超时，未获得该次检查的有效结果。',
            'failed':'检查失败，未获得该次检查的有效结果。'},
        'fact':'检查结果：服务 {service} 的状态为“{state}”。',
        'simulation':'来源：模拟检查，未访问真实实验机。',
        'gateway':'来源：本轮已授权 Gateway 检查；仅表示这次观察结果。',
        'rejected':'检查证据未通过校验，无法确认服务状态。',
        'unavailable':'本次未获得有效检查结果，无法判断服务状态。',
        'quote':'课程资料原文（第 {index} 条）：{excerpt}',
        'review':'建议：先对照以上课程资料核对当前步骤。',
        'more':'建议：现有证据不足以确定原因，需要进一步检查。',
        'omitted':'补充说明未通过校验，本次仅展示已校验的检查事实。'},
    'en': {
        'states': {'running':'running','stopped':'stopped','unknown':'unknown'},
        'failures': {
            'denied':'The check was not authorized; the tool was not executed.',
            'invalid':'The check parameters were invalid; the tool was not executed.',
            'budget_exhausted':'The tool-attempt budget has been reached.',
            'timeout':'The check timed out; no valid result was obtained for that attempt.',
            'failed':'The check failed; no valid result was obtained for that attempt.'},
        'fact':'Check result: service {service} is {state}.',
        'simulation':'Source: simulated check; no real lab instance was accessed.',
        'gateway':'Source: the authorized Gateway check for this run; a point-in-time observation only.',
        'rejected':'The check evidence failed validation; the service status cannot be confirmed.',
        'unavailable':'No valid check result was obtained; the service status cannot be determined.',
        'quote':'Course material verbatim (item {index}): {excerpt}',
        'review':'Suggestion: compare the current step with the course material above.',
        'more':'Suggestion: the evidence does not establish the cause; further checks are needed.',
        'omitted':'The additional explanation failed validation; only verified check facts are shown.'}
}



class ExplanationSelection(BaseModel):
    """模型只选证据、课程原文和有限建议；不接收未经校验的自由说明。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    evidence_ids: list[str] = Field(max_length=10)
    course_instruction_indexes: list[int] = Field(max_length=5)
    next_step: Literal['none', 'review_course', 'need_more_evidence']


def trusted_policy(request: CourseQuestion, bridge: dict) -> ToolPolicy:
    """在启动模型前校验课程绑定；运行ID和时限由运行适配器另行赋值。"""
    try:
        spec = BridgeSpec.model_validate(bridge)
        policy = ToolPolicy.model_validate({**spec.context['policy'], 'run_id': 'pending',
                                            'deadline_unix': 0.0})
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValueError('工具授权上下文缺失或格式不正确。') from exc
    if (policy.request_id, policy.course_id, policy.course_version) != (
            request.request_id, request.course.course_id, request.course.version):
        raise ValueError('工具授权上下文与当前请求、课程或版本不一致。')
    if policy.lesson_id is not None and policy.lesson_id != request.course.lesson_id:
        raise ValueError('工具授权课时与当前请求不一致。')
    if not policy.instance_id or not policy.student_id:
        raise ValueError('工具授权必须绑定学生和实例。')
    return policy


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('证据时间必须包含时区。')
    return parsed


def _validated_evidence(request, result, policy):
    """核对完整结果集合；发现错绑、冲突或伪造时整组拒绝，避免选择性泄漏。"""
    run_id = result['run_id']
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('missing_run')
    started = _timestamp(result['run_started_at'])
    now = datetime.now(timezone.utc)
    if started > now + timedelta(seconds=5):
        raise ValueError('future_run')
    raw_evidence, traces = result.get('evidence', []), result.get('tool_trace', [])
    if (not isinstance(raw_evidence, list) or not isinstance(traces, list)
            or not all(isinstance(row, dict) for row in traces)):
        raise ValueError('invalid_collections')
    evidence = [ServiceEvidence.model_validate(x) for x in raw_evidence]
    attempts, executions = result.get('tool_attempts', 0), result.get('tool_calls', 0)
    if (type(attempts) is not int or type(executions) is not int
            or not 0 <= executions <= min(attempts, policy.max_attempts)
            or attempts != len(traces)):
        raise ValueError('invalid_counts')
    if len(evidence) > executions:
        raise ValueError('missing_execution')
    by_id = {}
    by_service = {}
    for item in evidence:
        if (item.request_id, item.run_id, item.instance_id, item.course_id, item.course_version) != (
                request.request_id, run_id, policy.instance_id, request.course.course_id, request.course.version):
            raise ValueError('wrong_binding')
        if item.instance_generation != policy.instance_generation:
            raise ValueError('wrong_generation')
        if (TOOL_NAME not in policy.allowed_tools or item.service not in policy.allowed_services
                or item.source not in policy.allowed_sources):
            raise ValueError('outside_scope')
        observed = _timestamp(item.observed_at)
        if not started <= observed <= now + timedelta(seconds=5):
            raise ValueError('stale_or_future')
        if item.evidence_id in by_id:
            raise ValueError('duplicate_evidence')
        prior = by_service.get(item.service)
        if prior and (prior.state, prior.source) != (item.state, item.source):
            raise ValueError('conflicting_evidence')
        by_id[item.evidence_id] = item
        by_service[item.service] = item
    seen = set()
    for expected_attempt, row in enumerate(traces, start=1):
        if (row.get('request_id'), row.get('run_id'), row.get('instance_id')) != (
                request.request_id, run_id, policy.instance_id):
            raise ValueError('wrong_trace_binding')
        if type(row.get('attempt')) is not int or row['attempt'] != expected_attempt:
            raise ValueError('invalid_attempt_sequence')
        status = row.get('status')
        if status == 'ok':
            item = ServiceEvidence.model_validate(row['evidence'])
            if (row.get('tool') != TOOL_NAME or row['attempt'] > policy.max_attempts
                    or item.evidence_id not in by_id
                    or item != by_id[item.evidence_id] or item.evidence_id in seen):
                raise ValueError('invalid_success_trace')
            seen.add(item.evidence_id)
        elif status not in PRESENTATION['zh-CN']['failures'] or row.get('evidence') is not None:
            raise ValueError('invalid_failure_trace')
    if seen != set(by_id):
        raise ValueError('missing_success_trace')
    return evidence, list(by_service.values())


def compose_tool_answer(request: CourseQuestion, result: dict, policy: ToolPolicy) -> dict:
    """保持answer/evidence等已有字段；质量状态独立于completed，不自动重试模型。"""
    if request.course.locale not in PRESENTATION:
        raise ValueError('工具结果尚未支持该语言。')
    messages = PRESENTATION[request.course.locale]
    output = dict(result)
    draft = output.pop('answer', '')
    output.pop('run_started_at', None)  # 内部校验时间，不扩散为学生界面的新字段。
    quality = {'facts': 'unavailable', 'explanation': 'omitted', 'issues': []}
    output['quality'] = quality
    try:
        evidence, facts = _validated_evidence(request, result, policy)
    except (ValueError, TypeError, KeyError, ValidationError):
        # 不把其他学生的证据或畸形工具轨迹带回调用方；原始审计在运行目录保留。
        output.update(status='partial', answer=messages['rejected'],
                      evidence=[], tool_trace=[])
        quality.update(facts='rejected', issues=['evidence_rejected'])
        return output

    lines = []
    for fact in facts:
        service = json.dumps(fact.service, ensure_ascii=False)
        lines.append(messages['fact'].format(service=service,state=messages['states'][fact.state]))
        lines.append(messages['simulation'] if fact.source == 'simulation' else messages['gateway'])
    failures = list(dict.fromkeys(row['status'] for row in result.get('tool_trace', [])
                                  if row['status'] != 'ok'))
    lines.extend(messages['failures'][status] for status in failures)
    if not evidence:
        lines.append(messages['unavailable'])
        quality['issues'].append('no_valid_evidence')
    else:
        quality['facts'] = 'verified'
    if any(x.state == 'unknown' for x in facts):
        quality['issues'].append('state_unknown')
    if failures:
        quality['issues'].append('tool_incomplete')

    # 只验证“引用和选择是否合法”，不宣称自动证明课程相关性或复杂推理正确。
    try:
        selection = ExplanationSelection.model_validate(json.loads(draft))
        ids = [x.evidence_id for x in evidence]
        indexes = selection.course_instruction_indexes
        if (not ids or len(selection.evidence_ids) != len(set(selection.evidence_ids))
                or set(selection.evidence_ids) != set(ids)
                or len(indexes) != len(set(indexes))
                or any(type(i) is not int or not 0 <= i < len(request.course.instructions) for i in indexes)
                or (selection.next_step == 'review_course' and not indexes)):
            raise ValueError('invalid_selection')
        for index in indexes:
            # JSON引号限定原文边界；复制原文而不是让模型编造“课程要求”。
            excerpt = json.dumps(request.course.instructions[index], ensure_ascii=False)
            lines.append(messages['quote'].format(index=index+1,excerpt=excerpt))
        if selection.next_step == 'review_course':
            lines.append(messages['review'])
        elif selection.next_step == 'need_more_evidence':
            lines.append(messages['more'])
        quality['explanation'] = 'validated_selection'
    except (ValueError, TypeError, ValidationError):
        quality['issues'].append('explanation_omitted')
        if evidence:
            lines.append(messages['omitted'])

    output['answer'] = '\n'.join(lines)
    output['status'] = 'partial' if quality['issues'] else 'completed'
    return output
