"""与HTTP无关的记忆管理应用接口；可信身份独立于学生提交的数据。

正式平台可调用此接口或实现同等契约。这里只管理任务与显式自述，不能操作
环境、授予工具权限、调用模型或修改平台学习进度。
"""
from typing import Annotated, Literal, Union
from pydantic import BaseModel,ConfigDict,Field,TypeAdapter,ValidationError
from .storage import StorageError


class Command(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class ListTasks(Command):
    action:Literal['list_tasks']
    after:int=Field(default=0,ge=0)


class EnsureTask(Command):
    action:Literal['ensure_task']


class CreateTask(Command):
    action:Literal['create_task']
    task_id:str=Field(min_length=1,max_length=128)
    title:str=Field(min_length=1,max_length=100)


class ViewMemory(Command):
    action:Literal['view']
    task_id:str=Field(min_length=1,max_length=128)


class EditMemory(ViewMemory):
    action:Literal['edit']
    kind:Literal['goal','attempt','open_issue','preference']
    text:str=Field(min_length=1,max_length=300)
    expected_revision:int=Field(ge=0)
    request_id:str=Field(min_length=1,max_length=128)


class RevokeMemory(ViewMemory):
    action:Literal['revoke']
    kind:Literal['goal','attempt','open_issue','preference']
    expected_revision:int=Field(ge=1)


class Unassigned(Command):
    action:Literal['unassigned']


class LifecyclePreview(ViewMemory):
    action:Literal['preview_lifecycle']
    target:Literal['archived','active']


class LifecycleApply(LifecyclePreview):
    action:Literal['apply_lifecycle']
    preview_token:str=Field(min_length=64,max_length=64)
    operation_id:str=Field(min_length=1,max_length=128)


class InspectionList(ViewMemory):
    action:Literal['inspections']
    before:int=Field(default=9223372036854775807,ge=1,le=9223372036854775807)


class InspectionDetail(ViewMemory):
    action:Literal['inspection_detail']
    request_id:str=Field(min_length=1,max_length=128)


COMMANDS=TypeAdapter(Annotated[Union[ListTasks,EnsureTask,CreateTask,ViewMemory,EditMemory,RevokeMemory,Unassigned,LifecyclePreview,LifecycleApply,InspectionList,InspectionDetail],Field(discriminator='action')])


class TaskMemoryAPI:
    """不接受student_id或handle字段；调用方从已验证身份取得handle后单独传入。"""
    def __init__(self,repository):self.repository=repository

    def execute(self,trusted_handle,payload):
        try:cmd=COMMANDS.validate_python(payload)
        except (ValidationError,TypeError):raise StorageError('invalid_input') from None
        repo=self.repository
        if cmd.action=='list_tasks':return repo.list(trusted_handle,after=cmd.after)
        if cmd.action=='ensure_task':return repo.ensure_lesson_task(trusted_handle)
        if cmd.action=='create_task':return repo.create(trusted_handle,cmd.task_id,cmd.title)
        if cmd.action=='unassigned':return repo.unassigned(trusted_handle)
        if cmd.action in ('inspections','inspection_detail'):
            from .inspection_memory import InspectionRepository
            history=InspectionRepository(repo.store)
            if cmd.action=='inspections':return history.list(trusted_handle,cmd.task_id,before=cmd.before)
            return history.get(trusted_handle,cmd.task_id,cmd.request_id)
        if cmd.action in ('preview_lifecycle','apply_lifecycle'):
            from .lifecycle import TaskLifecycle
            lifecycle=TaskLifecycle(repo.store)
            if cmd.action=='preview_lifecycle':return lifecycle.preview(trusted_handle,cmd.task_id,cmd.target)
            return lifecycle.apply(trusted_handle,cmd.task_id,cmd.target,preview_token=cmd.preview_token,operation_id=cmd.operation_id)
        bound=repo.bind(trusted_handle,cmd.task_id)
        if cmd.action=='edit':
            bound.edit_learning(trusted_handle,cmd.kind,cmd.text,expected_revision=cmd.expected_revision,request_id=cmd.request_id)
        elif cmd.action=='revoke':
            bound.forget_learning(trusted_handle,cmd.kind,expected_revision=cmd.expected_revision)
        rows=bound.inspect(trusted_handle)
        # 展示摘录与有效项来自同一读取结果，不从模型自由总结，更不声称当前环境已验证。
        labels={'goal':'学生目标','attempt':'学生表示已尝试','open_issue':'学生尚未解决的问题','preference':'学生明确偏好'}
        task=bound.task_info(trusted_handle)
        brief='\n'.join(labels[r['kind']]+'：'+r['text'] for r in rows if r['state']=='active' and not task['archived'])
        return {'task_id':cmd.task_id,'task':task,'memory':rows,'brief':brief,'source':'student_statement',
                'model_calls':0,'note':'这是有效自述摘录；实际模型输入仍按总预算筛选。'}
