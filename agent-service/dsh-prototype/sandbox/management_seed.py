"""固定演示库，仅生成模拟诊断；不调用模型、不写真实学生数据。"""
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.conversation import ConversationScope
from dsh_tutor.task_memory import migrate_task_memory
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.inspection_memory import migrate_inspections
from dsh_tutor.diagnostics import DiagnosticStore,RequestTrace
root=Path(__file__).resolve().parents[1]/'sandbox/results/management-demo'
root.mkdir(parents=True,exist_ok=True)
p=root/'fictional.db'
store=SQLiteChatStore(p) if p.exists() else SQLiteChatStore.create(p)
store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store);migrate_inspections(store)
scope=ConversationScope(student_id='fictional-management',learning_context_id='fictional-management',course_id='fictional-course',course_version='demo-v1',lesson_id='demo-lesson')
handle=store.activate(scope)
diag=DiagnosticStore(store)
if not diag.read_page(handle)['events']:
    for key,state,stage,code in [('simulated-timeout','failed','model','timeout'),('simulated-save','not_saved','save','unavailable'),('simulated-success','saved','save',None)]:
        trace=RequestTrace();trace.mark('history');trace.mark(stage)
        diag.record(handle,SimpleNamespace(request_id=key,task_id=None,state=state,stage=stage,error_code=code),trace)
print('固定模拟诊断已准备；0次模型请求。')
