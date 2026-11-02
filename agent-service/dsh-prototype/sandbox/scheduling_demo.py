"""有限的一轮虚构多人流量：实际问答/SQLite/调度，替换模型传输，绝不调用Mac。

只由显式--scheduler-demo启动。夹具在sandbox，正式模块不导入这里。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, Event
from unittest.mock import patch
from uuid import uuid4

from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.conversation import ConversationScope
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.task_memory import migrate_task_memory,TaskRepository
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.resources import ResourceManagedTutor


class SchedulingDemo:
    def __init__(self,root):
        self.root=Path(root)/'sandbox/results/scheduling-demo'/uuid4().hex
        self.root.mkdir(parents=True)
        self.database=self.root/'fictional.db';self.store=SQLiteChatStore.create(self.database)
        self.store.migrate_learning_memory();migrate_task_memory(self.store);migrate_lifecycle(self.store)
        self.service=ResourceManagedTutor(policy={'min_free_bytes':0},config_path='not-used-by-mock',
            state_dir=self.root,diagnostics=DiagnosticStore(self.store))
        repo=TaskRepository(self.store);self.inputs=[];self._stop=Event();self._thread=None
        for i in range(8):
            s=ConversationScope(student_id=f'fictional-{i}',learning_context_id=f'enrollment-{i}',
                course_id='course-a',course_version='v1',lesson_id='l1',instance_id=f'vm-{i}',
                instance_generation='g1',environment_os='linux' if i%2 else 'windows')
            h=self.store.activate(s);repo.create(h,'task','虚构调度任务')
            q=CourseQuestion(request_id=f'demo-{i}',question='这是虚构调度测试。',
                course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
            self.inputs.append((q,repo.bind(h,'task'),h))

    def start(self):
        def fake_model(*args,**kwargs):
            self._stop.wait(3)
            return dict(answer='虚构模型回答，仅用于调度演示。',model='simulation',run_id=uuid4().hex,
                        run_started_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=3.0)
        def traffic():
            with patch('dsh_tutor.runtime.run_prompt',side_effect=fake_model):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures=[pool.submit(self.service.answer,q,store=s,handle=h) for q,s,h in self.inputs]
                    for f in futures:f.result()
        self._thread=Thread(target=traffic,name='fictional-scheduler-traffic',daemon=True);self._thread.start()
        return self

    def close(self):
        self._stop.set();self.service.gate.close()
        if self._thread is not None:self._thread.join(5)
