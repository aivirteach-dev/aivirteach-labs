"""任务生命周期：预览和版本核对、幂等归档/恢复；不删除记录，不调用模型。"""
import hashlib
import json
from pydantic import BaseModel,ConfigDict,Field
from .storage import StorageError,_handle,_text,_now
from .task_memory import TaskRepository,_advance


class LifecycleLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_operations_per_context:int=Field(default=1000,ge=1,le=10000)


def migrate_lifecycle(store):
    """可信应用显式v3→v4；旧库不自动改，存在pending时拒绝。"""
    with store._connection(write=True) as db:
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version in (4,5):return
        if version!=3:raise StorageError('task_migration_required')
        if db.execute("SELECT 1 FROM turns WHERE status='pending' LIMIT 1").fetchone():raise StorageError('requests_pending')
        db.execute('ALTER TABLE learning_tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))')
        db.execute('ALTER TABLE learning_tasks ADD COLUMN lifecycle_revision INTEGER NOT NULL DEFAULT 1')
        db.execute('''CREATE TABLE lifecycle_operations (
            context_id TEXT NOT NULL REFERENCES contexts(context_id),operation_id TEXT NOT NULL,
            task_id TEXT NOT NULL,target TEXT NOT NULL,preview_token TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,
            PRIMARY KEY(context_id,operation_id))''')
        db.execute('''CREATE TABLE diagnostic_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,context_id TEXT NOT NULL REFERENCES contexts(context_id),
            request_ref TEXT NOT NULL,task_ref TEXT,state TEXT NOT NULL,stage TEXT NOT NULL,error_code TEXT,
            elapsed_seconds REAL NOT NULL,timeline_json TEXT NOT NULL,created_at TEXT NOT NULL)''')
        db.execute('CREATE INDEX diagnostic_context_page ON diagnostic_events(context_id,sequence)')
        db.execute('PRAGMA user_version=4')


def require_lifecycle(db):
    if db.execute('PRAGMA user_version').fetchone()[0] not in (4,5):raise StorageError('lifecycle_migration_required')


class TaskLifecycle:
    def __init__(self,store,limits=None):
        self.store=store;self.limits=LifecycleLimits.model_validate({} if limits is None else limits)

    def _task(self,db,handle,task_id):
        require_lifecycle(db);self.store._check(db,handle,active=True)
        row=db.execute('SELECT * FROM learning_tasks WHERE context_id=? AND task_id=?',
                       (handle.scope.learning_context_id,task_id)).fetchone()
        TaskRepository._matches(row,handle);return row

    def _preview(self,db,handle,task_id,target):
        if target not in ('archived','active'):raise StorageError('invalid_input')
        row=self._task(db,handle,task_id);key=handle.scope.learning_context_id
        counts=db.execute("SELECT COUNT(*),COALESCE(MAX(a.sequence),0),COALESCE(SUM(t.status='pending'),0) "
                          'FROM turn_tasks a JOIN turns t ON t.sequence=a.sequence WHERE a.context_id=? AND a.task_id=?',
                          (key,task_id)).fetchone()
        epoch=db.execute('SELECT epoch FROM task_memory_state WHERE context_id=? AND task_id=?',(key,task_id)).fetchone()
        identity=[handle.model_dump(),task_id,target,row['lifecycle_revision'],counts[1],epoch[0] if epoch else 0]
        token=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'task':TaskRepository._task(row),'target':target,'turns':counts[0],'pending':counts[2],
                'preview_token':token,'shared_preference_preserved':True,'reclaimed_bytes':0,
                'note':'归档只影响默认使用范围，不删除原文、不释放磁盘；恢复不重置记忆有效性。'}

    def preview(self,handle,task_id,target):
        handle=_handle(handle);_text(task_id,128)
        with self.store._connection() as db:return self._preview(db,handle,task_id,target)

    def apply(self,handle,task_id,target,*,preview_token,operation_id):
        """同一写事务串行核对pending/预览/范围；请求接受与归档不能穿过彼此检查。"""
        handle=_handle(handle);_text(task_id,128);_text(operation_id,128);_text(preview_token,64)
        with self.store._connection(write=True) as db:
            self._task(db,handle,task_id);key=handle.scope.learning_context_id
            old=db.execute('SELECT * FROM lifecycle_operations WHERE context_id=? AND operation_id=?',(key,operation_id)).fetchone()
            if old:
                if (old['task_id'],old['target'],old['preview_token'])!=(task_id,target,preview_token):raise StorageError('operation_conflict')
                return json.loads(old['result_json'])|{'replayed':True}
            current=self._preview(db,handle,task_id,target)
            if current['preview_token']!=preview_token:raise StorageError('preview_changed')
            if current['pending']:raise StorageError('task_busy')
            if db.execute('SELECT COUNT(*) FROM lifecycle_operations WHERE context_id=?',(key,)).fetchone()[0]>=self.limits.max_operations_per_context:
                raise StorageError('capacity')
            archived=target=='archived';changed=current['task']['archived']!=archived
            if changed:
                db.execute('UPDATE learning_tasks SET archived=?,lifecycle_revision=lifecycle_revision+1 WHERE context_id=? AND task_id=?',
                           (int(archived),key,task_id))
                state=db.execute('SELECT history_floor FROM task_memory_state WHERE context_id=? AND task_id=?',(key,task_id)).fetchone()
                _advance(db,key,task_id,state[0] if state else 0)
            result={'task_id':task_id,'archived':archived,'lifecycle_revision':current['task']['lifecycle_revision']+int(changed),'replayed':False,'changed':changed}
            db.execute('INSERT INTO lifecycle_operations VALUES(?,?,?,?,?,?,?)',
                       (key,operation_id,task_id,target,preview_token,json.dumps(result),_now()))
            return result
