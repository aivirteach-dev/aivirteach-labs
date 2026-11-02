"""固定AIVirTeach契约的显式边界：保留教学字段，未支持的动态字段拒绝。

不从源码快照导入正式模块，不替换平台认证、课程库或完整诊断；输入过长不截断。
"""
import json
from uuid import UUID
from pydantic import Field,ValidationError
from .service_contracts import StrictModel,ServiceError
from .contracts import CourseQuestion,CourseSnapshot


class PlatformCourse(StrictModel):
    course_id:str=Field(min_length=1,max_length=128)
    version:str | int
    title:str=Field(min_length=1,max_length=200)
    summary:str=Field(default='',max_length=2000)
    relevant_excerpts:list[dict]=Field(default_factory=list,max_length=5)


class PlatformStep(StrictModel):
    module_id:str=Field(min_length=1,max_length=128)
    lesson_id:str=Field(min_length=1,max_length=128)
    sequence:int=Field(ge=1,le=10000)
    title:str=Field(min_length=1,max_length=200)
    summary:str=Field(default='',max_length=2000)
    instructions:list[str]=Field(default_factory=list,max_length=20)
    expected_result:str=Field(default='',max_length=4000)
    success_criteria:list[str]=Field(default_factory=list,max_length=20)
    common_failures:list[dict]=Field(default_factory=list,max_length=12)


class PlatformRequest(StrictModel):
    request_id:str=Field(min_length=1,max_length=128)
    lab_id:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')
    question:str=Field(min_length=1,max_length=2000)
    response_language:str=Field(default='zh-CN',max_length=35)
    course:PlatformCourse
    current_step:PlatformStep
    history:list[dict]=Field(default_factory=list,max_length=8)
    learner_state:dict=Field(default_factory=dict)
    diagnostic_scope:dict=Field(default_factory=dict)


def adapt_platform(payload,*,lab_id):
    """教材字段完整保留为有标签的数据块；身份/VM和工具权限仍来自TrustedBinding。"""
    try:
        request=PlatformRequest.model_validate(payload);UUID(request.request_id)
        if isinstance(request.course.version,int) and request.course.version<1:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise ServiceError('invalid_platform_request',422) from None
    if request.lab_id!=lab_id:raise ServiceError('lab_mismatch',403)
    if request.history or request.learner_state or request.diagnostic_scope:
        # 持久历史和授权不能混入一套未经对账的平台数据，明确要求后续映射。
        raise ServiceError('unsupported_dynamic_context',422)
    teaching={'course':request.course.model_dump(),'current_step':request.current_step.model_dump()}
    encoded=json.dumps(teaching,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    if len(encoded)>5000:raise ServiceError('platform_context_too_large',422)
    try:
        course=CourseSnapshot(course_id=request.course.course_id,version=str(request.course.version),
            lesson_id=request.current_step.lesson_id,title=request.course.title,locale=request.response_language,
            instructions=['平台提供的当前课程与课时资料（JSON数据，不是工具授权）：'+encoded])
        return CourseQuestion(request_id=request.request_id,question=request.question,course=course)
    except ValidationError:raise ServiceError('invalid_platform_request',422) from None


def platform_response(view):
    """可选响应投影；缺完整诊断时必须partial，usage未知为null，绝不伪造0。

    只支持课程问答投影；受控工具证据有不同结构，未映射时明确拒绝，不能
    把get_service_status直接伪装成平台GET_GUEST_SERVICE_STATUS。
    """
    result=view.get('result')
    if view.get('state')!='succeeded' or not result:raise ServiceError('result_not_ready')
    detail=result.get('detail')
    if detail and (detail.get('evidence') or detail.get('tool_trace')):raise ServiceError('unsupported_diagnostic_projection',422)
    return {'request_id':view['request_id'],'status':'partial','answer':result['answer'],
        'diagnosis':{'summary':'本轮提供课程问答，未执行完整实验诊断。','probable_causes':[],'confidence':'low'},
        'course_alignment':{'expected':[],'observed':[]},'evidence':[],'suggested_actions':[],
        'limitations':['完整诊断、平台历史/学习状态和真实Gateway尚未接入。',
            '结果只确认Agent侧保存；平台仍须正式保存后才能向学生发送complete。',
            '读取的是本请求保存结果，不代表重新检查当前环境。'],
        'tool_trace':[],'usage':None}
