"""任务记忆适配：显式任务随请求传递，复用已有问答和SQLite事务内核。

不从自然语言猜测任务，不新增模型调用。此模块是本地SQLite适配；正式平台
以后实现相同行为接口，不要求照搬表结构。身份/handle必须来自可信应用。
"""
from hashlib import sha256
from pydantic import BaseModel, ConfigDict, Field

from .storage import StorageError, TurnPage, _handle, _text, _now
from .learning_memory import LearningItem, LearningBrief, MemoryLimits, PREFIXES, explicit_statements


class TaskLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_tasks:int=Field(default=100,ge=1,le=1000)
    page_limit:int=Field(default=20,ge=1,le=100)


TASK_SCHEMA='''
CREATE TABLE learning_tasks (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 context_id TEXT NOT NULL REFERENCES contexts(context_id), task_id TEXT NOT NULL,
 course_id TEXT NOT NULL, course_version TEXT NOT NULL, lesson_id TEXT NOT NULL,
 title TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(context_id,task_id)
);
CREATE INDEX tasks_by_lesson ON learning_tasks(context_id,course_id,course_version,lesson_id,sequence);
CREATE TABLE turn_tasks (
 context_id TEXT NOT NULL, request_id TEXT NOT NULL, task_id TEXT NOT NULL,
 scope_revision INTEGER NOT NULL, sequence INTEGER NOT NULL UNIQUE REFERENCES turns(sequence),
 PRIMARY KEY(context_id,request_id),
 FOREIGN KEY(context_id,request_id) REFERENCES turns(context_id,request_id),
 FOREIGN KEY(context_id,task_id) REFERENCES learning_tasks(context_id,task_id)
);
CREATE INDEX task_turn_page ON turn_tasks(context_id,task_id,scope_revision,sequence);
CREATE TABLE task_learning_memory (
 context_id TEXT NOT NULL REFERENCES contexts(context_id), task_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('goal','attempt','open_issue','preference')),
 revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','needs_review','revoked')),
 scope_revision INTEGER NOT NULL, source_request_id TEXT NOT NULL,
 source_start INTEGER NOT NULL, source_end INTEGER NOT NULL, quote TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(context_id,task_id,kind),
 FOREIGN KEY(context_id,source_request_id) REFERENCES turns(context_id,request_id),
 CHECK((kind='preference' AND task_id='') OR (kind!='preference' AND task_id!=''))
);
CREATE TABLE task_memory_clock (context_id TEXT PRIMARY KEY REFERENCES contexts(context_id), epoch INTEGER NOT NULL);
CREATE TABLE task_memory_state (
 context_id TEXT NOT NULL REFERENCES contexts(context_id),task_id TEXT NOT NULL,
 history_floor INTEGER NOT NULL,epoch INTEGER NOT NULL,PRIMARY KEY(context_id,task_id)
);
'''


def migrate_task_memory(store):
    """显式v2→v3事务迁移。旧消息和旧记忆保持未归类，不猜测所属任务。"""
    with store._connection(write=True) as db:
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version in (3,4,5):return
        if version!=2:raise StorageError('memory_migration_required')
        if db.execute("SELECT 1 FROM turns WHERE status='pending' LIMIT 1").fetchone():
            raise StorageError('requests_pending')
        for statement in TASK_SCHEMA.split(';'):
            if statement.strip():db.execute(statement)
        db.execute('PRAGMA user_version=3')


def _schema(db):
    if db.execute('PRAGMA user_version').fetchone()[0] not in (3,4,5):
        raise StorageError('task_migration_required')


def _floor(db,key):
    return db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM turns WHERE context_id=?',(key,)).fetchone()[0]


def _advance(db,key,task_id,floor):
    """上下文时钟只分配单调版本；不同任务各有下界，偏好变更影响该学习记录所有任务。"""
    db.execute('INSERT INTO task_memory_clock VALUES(?,1) ON CONFLICT(context_id) DO UPDATE SET epoch=epoch+1',(key,))
    epoch=db.execute('SELECT epoch FROM task_memory_clock WHERE context_id=?',(key,)).fetchone()[0]
    db.execute('INSERT INTO task_memory_state VALUES(?,?,?,?) ON CONFLICT(context_id,task_id) '
               'DO UPDATE SET history_floor=MAX(history_floor,excluded.history_floor),epoch=excluded.epoch',
               (key,task_id,floor,epoch))


def invalidate_history(db,key,scope_revision):
    """原聊天删除与任务记忆撤销在同一事务，连带失效跨环境引用的来源。"""
    rows=db.execute('SELECT DISTINCT task_id FROM task_learning_memory WHERE context_id=? '
                    'AND source_request_id IN (SELECT request_id FROM turns WHERE context_id=? AND scope_revision=?)',
                    (key,key,scope_revision)).fetchall()
    db.execute("UPDATE task_learning_memory SET state='revoked',quote='',revision=revision+1,updated_at=? "
               'WHERE context_id=? AND source_request_id IN (SELECT request_id FROM turns WHERE context_id=? AND scope_revision=?)',
               (_now(),key,key,scope_revision))
    for row in rows:_advance(db,key,row[0],_floor(db,key))


class TaskRepository:
    """可信应用的任务入口；没有单个学生全局current_task，支持多个页面各自绑定。"""
    def __init__(self,store,limits=None):
        self.store=store
        self.limits=TaskLimits.model_validate({} if limits is None else limits)

    def create(self,handle,task_id,title):
        handle=_handle(handle);_text(task_id,128);_text(title,100)
        scope=handle.scope;key=scope.learning_context_id
        with self.store._connection(write=True) as db:
            _schema(db);self.store._check(db,handle,active=True)
            row=db.execute('SELECT * FROM learning_tasks WHERE context_id=? AND task_id=?',(key,task_id)).fetchone()
            if row:
                self._matches(row,handle)
                if row['title']!=title:raise StorageError('task_conflict')
                return self._task(row)
            if db.execute('SELECT COUNT(*) FROM learning_tasks WHERE context_id=?',(key,)).fetchone()[0]>=self.limits.max_tasks:
                raise StorageError('capacity')
            db.execute('INSERT INTO learning_tasks(context_id,task_id,course_id,course_version,lesson_id,title,created_at) '
                       'VALUES(?,?,?,?,?,?,?)',(key,task_id,scope.course_id,scope.course_version,scope.lesson_id,title,_now()))
            return self._task(db.execute('SELECT * FROM learning_tasks WHERE context_id=? AND task_id=?',(key,task_id)).fetchone())

    def ensure_lesson_task(self,handle):
        """没有独立练习标识时，用当前课时稳定默认任务；重新进入不会每次新建。"""
        scope=_handle(handle).scope
        identity='\x1f'.join((scope.course_id,scope.course_version,scope.lesson_id))
        task_id='lesson-'+sha256(identity.encode()).hexdigest()[:24]
        return self.create(handle,task_id,'当前课时练习')

    @staticmethod
    def _matches(row,handle):
        scope=handle.scope
        if row is None or (row['course_id'],row['course_version'],row['lesson_id'])!=(scope.course_id,scope.course_version,scope.lesson_id):
            raise StorageError('task_denied')

    @staticmethod
    def _task(row):
        result={k:row[k] for k in ('sequence','task_id','title','course_id','course_version','lesson_id','created_at')}
        result.update(archived=bool(row['archived']) if 'archived' in row.keys() else False,
                      lifecycle_revision=row['lifecycle_revision'] if 'lifecycle_revision' in row.keys() else 0)
        return result

    def list(self,handle,*,after=0,limit=None):
        handle=_handle(handle)
        limit=self.limits.page_limit if limit is None else limit
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=self.limits.page_limit:
            raise StorageError('invalid_input')
        s=handle.scope
        with self.store._connection() as db:
            _schema(db);self.store._check(db,handle,active=True)
            rows=db.execute('SELECT * FROM learning_tasks WHERE context_id=? AND course_id=? AND course_version=? '
                            'AND lesson_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                            (s.learning_context_id,s.course_id,s.course_version,s.lesson_id,after,limit+1)).fetchall()
            return {'tasks':[self._task(r) for r in rows[:limit]],'next_after':rows[limit-1]['sequence'] if len(rows)>limit else None}

    def bind(self,handle,task_id):
        return TaskMemoryStore(self.store,handle,task_id)

    def unassigned(self,handle):
        """只读兼容入口，不把旧快照偷偷变成所有新任务的共享记忆。"""
        handle=_handle(handle);key=handle.scope.learning_context_id
        with self.store._connection() as db:
            _schema(db);self.store._check(db,handle,active=True)
            count=db.execute('SELECT COUNT(*) FROM turns t WHERE t.context_id=? AND NOT EXISTS '
                             '(SELECT 1 FROM turn_tasks a WHERE a.context_id=t.context_id AND a.request_id=t.request_id)',(key,)).fetchone()[0]
            rows=db.execute("SELECT kind,revision,quote FROM learning_memory WHERE context_id=? AND active=1 "
                            'AND slot_scope IN (0,?) ORDER BY kind LIMIT 4',(key,handle.revision)).fetchall()
            return {'turns':count,'memory':[dict(r) for r in rows],'used_in_task_prompt':False}


class TaskMemoryStore:
    """不可变任务绑定，既实现ChatStore问答契约，也实现学习记忆契约。

    不另写Agent循环。实现问答所需的存取子集，传给answer_chat或ResourceManagedTutor作为store与learning_memory。
    切换页面任务只创建新绑定，不修改其他请求的绑定。
    """
    __slots__=('_store','_handle','_task_id')

    def __init__(self,store,handle,task_id):
        self._store=store;self._handle=_handle(handle);self._task_id=_text(task_id,128)
        with store._connection() as db:self._check(db,self.handle,allow_archived=True)

    @property
    def handle(self):return self._handle

    @property
    def task_id(self):return self._task_id

    @property
    def learning_memory_required(self):
        """任务问答不能因调用方漏传learning_memory而绕过记忆版本保护。"""
        return True

    def _check(self,db,handle,*,allow_archived=False):
        _schema(db);handle=_handle(handle)
        if handle!=self.handle:raise StorageError('task_denied')
        self._store._check(db,handle,active=True)
        row=db.execute('SELECT * FROM learning_tasks WHERE context_id=? AND task_id=?',
                       (handle.scope.learning_context_id,self.task_id)).fetchone()
        TaskRepository._matches(row,handle)
        if 'archived' in row.keys() and row['archived'] and not allow_archived:
            raise StorageError('task_archived')
        return row

    def task_info(self,handle):
        """管理元数据，不扩大任务读取范围。"""
        with self._store._connection() as db:
            return TaskRepository._task(self._check(db,handle,allow_archived=True))

    @property
    def inspection_required(self):return self._store.inspection_required

    @property
    def page_capacity(self):return self._store.page_capacity

    def current(self,**kwargs):return self._store.current(**kwargs)

    def accept(self,handle,request_id,question):
        with self._store._connection(write=True) as db:
            self._check(db,handle);key=handle.scope.learning_context_id
            old=db.execute('SELECT task_id FROM turn_tasks WHERE context_id=? AND request_id=?',(key,request_id)).fetchone()
            exists=db.execute('SELECT 1 FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone()
            if exists and (old is None or old[0]!=self.task_id):raise StorageError('request_conflict')
            accepted=self._store._accept_in(db,handle,request_id,question)
            if accepted.created:
                db.execute('INSERT INTO turn_tasks VALUES(?,?,?,?,?)',(key,request_id,self.task_id,handle.revision,accepted.turn.sequence))
            return accepted

    def begin(self,handle,request_id,question):return self.accept(handle,request_id,question).turn

    def _source(self,db,handle,rid):
        key=handle.scope.learning_context_id
        row=db.execute('SELECT t.* FROM turns t JOIN turn_tasks a ON a.context_id=t.context_id AND a.request_id=t.request_id '
                       'WHERE t.context_id=? AND t.request_id=? AND a.task_id=? AND a.scope_revision=?',
                       (key,rid,self.task_id,handle.revision)).fetchone()
        if row is None:raise StorageError('request_not_found')
        return row

    def check_pending(self,handle,request_id,sequence,question):
        """重核课程/任务/环境、归档和原pending；不会切换任务或重新接受请求。"""
        with self._store._connection() as db:
            self._check(db,handle)
            self._store._pending_matches(self._source(db,handle,request_id),sequence,question)

    def finish(self,handle,request_id,*,expected_memory_epoch=None,**values):
        with self._store._connection(write=True) as db:
            self._check(db,handle);self._source(db,handle,request_id)
            if expected_memory_epoch is not None:
                if type(expected_memory_epoch) is not int or expected_memory_epoch<0:raise StorageError('invalid_input')
                if self._state(db,handle)[1]!=expected_memory_epoch:raise StorageError('memory_changed')
            return self._store._finish_in(db,handle,request_id,**values)

    def get_request(self,handle,request_id):
        """对账/交付只读查询仍限定当前学生、任务与环境；归档可查看历史。"""
        with self._store._connection() as db:
            self._check(db,handle,allow_archived=True)
            row=db.execute('SELECT t.* FROM turns t JOIN turn_tasks a ON a.context_id=t.context_id AND a.request_id=t.request_id '
                'WHERE t.context_id=? AND t.request_id=? AND a.task_id=? AND a.scope_revision=?',
                (handle.scope.learning_context_id,request_id,self.task_id,handle.revision)).fetchone()
            return self._store._turn(row) if row else None

    def read_page(self,handle,*,before=None,limit=None):
        limit=self.page_capacity if limit is None else limit
        if type(limit) is not int or not 1<=limit<=self.page_capacity or (before is not None and (type(before) is not int or before<1)):
            raise StorageError('invalid_input')
        with self._store._connection() as db:
            self._check(db,handle,allow_archived=True)
            if db.execute('PRAGMA user_version').fetchone()[0]==5:
                rows=db.execute("SELECT t.*,CASE WHEN r.kind='no_tools' THEN 0 ELSE 1 END AS history_excluded, CASE WHEN r.kind='no_tools' THEN r.body_json ELSE NULL END AS inspection_body,r.body_sha256 AS inspection_hash FROM turn_tasks a JOIN turns t ON t.sequence=a.sequence "
                                'LEFT JOIN inspection_records r ON r.context_id=t.context_id AND r.request_id=t.request_id '
                                'WHERE a.context_id=? AND a.task_id=? AND a.scope_revision=? AND a.sequence<? '
                                "AND (t.error_code IS NULL OR t.error_code!='history_deleted') ORDER BY a.sequence DESC LIMIT ?",
                                (handle.scope.learning_context_id,self.task_id,handle.revision,before or 9223372036854775807,limit+1)).fetchall()
            else:
                rows=db.execute('SELECT t.* FROM turn_tasks a JOIN turns t ON t.sequence=a.sequence '
                                'WHERE a.context_id=? AND a.task_id=? AND a.scope_revision=? AND a.sequence<? '
                                "AND (t.error_code IS NULL OR t.error_code!='history_deleted') ORDER BY a.sequence DESC LIMIT ?",
                                (handle.scope.learning_context_id,self.task_id,handle.revision,before or 9223372036854775807,limit+1)).fetchall()
            selected=rows[:limit]
            return TurnPage(turns=[self._store._turn(r) for r in reversed(selected)],
                            next_before=selected[-1]['sequence'] if len(rows)>limit else None)

    def _state(self,db,handle):
        rows=db.execute('SELECT history_floor,epoch FROM task_memory_state WHERE context_id=? AND task_id IN (?,?)',
                        (handle.scope.learning_context_id,'',self.task_id)).fetchall()
        return (max((r[0] for r in rows),default=0),max((r[1] for r in rows),default=0))

    def learning_state(self,handle):
        with self._store._connection() as db:
            self._check(db,handle);return self._state(db,handle)

    def _put(self,db,handle,rid,kind,start,end,text):
        key=handle.scope.learning_context_id;slot='' if kind=='preference' else self.task_id
        old=db.execute('SELECT * FROM task_learning_memory WHERE context_id=? AND task_id=? AND kind=?',(key,slot,kind)).fetchone()
        if old and old['state']=='active' and old['quote']==text and (kind in ('goal','preference') or old['scope_revision']==handle.revision):
            return
        if kind=='goal' and old and old['state']!='revoked' and old['quote']!=text:
            # 修改目标不等于新任务；旧尝试先待确认，不猜测其仍适用于新目标。
            db.execute("UPDATE task_learning_memory SET state='needs_review',revision=revision+1,updated_at=? "
                       "WHERE context_id=? AND task_id=? AND kind IN ('attempt','open_issue') AND state='active'",(_now(),key,slot))
        revision=old['revision']+1 if old else 1
        db.execute('INSERT INTO task_learning_memory VALUES(?,?,?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(context_id,task_id,kind) DO UPDATE SET revision=excluded.revision,state=excluded.state,'
                   'scope_revision=excluded.scope_revision,source_request_id=excluded.source_request_id,'
                   'source_start=excluded.source_start,source_end=excluded.source_end,quote=excluded.quote,updated_at=excluded.updated_at',
                   (key,slot,kind,revision,'active',handle.revision,rid,start,end,text,_now()))
        _advance(db,key,slot,_floor(db,key))

    def remember_question(self,handle,request_id,question,*,limits):
        limits=MemoryLimits.model_validate(limits.model_dump())
        try:statements=explicit_statements(question,limits)
        except ValueError:raise StorageError('invalid_memory_statement') from None
        # 同一问题同时声明新目标和尝试时，先处理目标，再处理明确的新尝试。
        statements.sort(key=lambda x:0 if x[0]=='goal' else 1)
        with self._store._connection(write=True) as db:
            self._check(db,handle);source=self._source(db,handle,request_id)
            if source['question']!=question or source['status']!='pending':raise StorageError('memory_source_rejected')
            for kind,start,end,text in statements:self._put(db,handle,request_id,kind,start,end,text)
            return self._items(db,handle)

    def _rows(self,db,handle):
        rows=db.execute('SELECT m.*,t.question,t.error_code,t.created_at,t.scope_revision AS source_scope,a.task_id AS source_task '
                        'FROM task_learning_memory m JOIN turns t '
                        'ON t.context_id=m.context_id AND t.request_id=m.source_request_id '
                        'LEFT JOIN turn_tasks a ON a.context_id=t.context_id AND a.request_id=t.request_id '
                        'WHERE m.context_id=? AND m.task_id IN (?,?) ORDER BY m.kind LIMIT 5',
                        (handle.scope.learning_context_id,'',self.task_id)).fetchall()
        if len(rows)>4:raise StorageError('invalid_memory')
        for row in rows:
            if row['state']!='revoked' and (row['error_code']=='history_deleted' or not row['quote']
                    or row['question'][row['source_start']:row['source_end']]!=row['quote']
                    or row['source_scope']!=row['scope_revision'] or row['source_task'] is None
                    or (row['kind']!='preference' and row['source_task']!=row['task_id'])):
                raise StorageError('memory_source_rejected')
        return rows

    @staticmethod
    def _effective(row,handle):
        if row['state']!='active':return row['state']
        if row['kind'] in ('attempt','open_issue') and row['scope_revision']!=handle.revision:return 'stale_environment'
        return 'active'

    def _items(self,db,handle):
        return [LearningItem(kind=r['kind'],revision=r['revision'],scope_revision=0 if r['kind']=='preference' else r['scope_revision'],
                            source_request_id=r['source_request_id'],source_start=r['source_start'],source_end=r['source_end'],
                            text=r['quote'],task_id=r['task_id'] or None,updated_at=r['updated_at'])
                for r in self._rows(db,handle) if self._effective(r,handle)=='active']

    def learning_items(self,handle):
        with self._store._connection() as db:
            task=self._check(db,handle,allow_archived=True)
            return [] if 'archived' in task.keys() and task['archived'] else self._items(db,handle)

    def learning_brief(self,handle):
        items=self.learning_items(handle)
        labels={'goal':'学生目标','attempt':'学生表示已尝试','open_issue':'学生尚未解决的问题','preference':'学生明确偏好'}
        return LearningBrief(text='\n'.join(labels[x.kind]+'：'+x.text for x in items),items=items)

    def inspect(self,handle):
        """学生管理视图，不混入其他任务；撤销项不返回原文。来源只代表自述。"""
        with self._store._connection() as db:
            self._check(db,handle,allow_archived=True)
            return [{'kind':r['kind'],'revision':r['revision'],'state':self._effective(r,handle),'text':r['quote'],
                     'task_id':r['task_id'] or None,'updated_at':r['updated_at'],'source_request_id':r['source_request_id'],
                     'source_start':r['source_start'],'source_end':r['source_end'],'source_time':r['created_at'],
                     'source':'student_statement'} for r in self._rows(db,handle)]

    def _expect(self,db,handle,kind,expected_revision):
        if kind not in PREFIXES.values() or type(expected_revision) is not int or expected_revision<0:raise StorageError('invalid_input')
        slot='' if kind=='preference' else self.task_id
        row=db.execute('SELECT revision FROM task_learning_memory WHERE context_id=? AND task_id=? AND kind=?',
                       (handle.scope.learning_context_id,slot,kind)).fetchone()
        if (row[0] if row else 0)!=expected_revision:raise StorageError('revision_conflict')
        return slot

    def edit_learning(self,handle,kind,text,*,expected_revision,request_id):
        """可信管理接口；以学生显式编辑原文为来源，CAS拒绝旧页面覆盖，绝不调用模型。"""
        _text(text,300);_text(request_id,128)
        prefix=next((p for p,k in PREFIXES.items() if k==kind),None)
        if prefix is None or '\n' in text or '\r' in text:raise StorageError('invalid_input')
        question=prefix+text;key=handle.scope.learning_context_id
        with self._store._connection(write=True) as db:
            self._check(db,handle);self._expect(db,handle,kind,expected_revision)
            if db.execute('SELECT 1 FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone():
                raise StorageError('request_conflict')
            if db.execute('SELECT COUNT(*) FROM turns WHERE context_id=?',(key,)).fetchone()[0]>=self._store.limits.max_turns_per_context:
                raise StorageError('capacity')
            # 管理事件不是模型问答：没有伪造的助手回答，历史恢复会跳过此状态。
            now=_now()
            cur=db.execute("INSERT INTO turns(context_id,scope_revision,request_id,question,status,error_code,created_at,updated_at) "
                           "VALUES(?,?,?,?,'cancelled','memory_edit',?,?)",(key,handle.revision,request_id,question,now,now))
            db.execute('INSERT INTO turn_tasks VALUES(?,?,?,?,?)',(key,request_id,self.task_id,handle.revision,cur.lastrowid))
            self._put(db,handle,request_id,kind,len(prefix),len(question),text)
        return self.inspect(handle)

    def forget_learning(self,handle,kind,*,expected_revision):
        """逻辑撤销及下界/版本更新同一事务；原聊天仍保留，不承诺安全擦除。"""
        with self._store._connection(write=True) as db:
            self._check(db,handle);slot=self._expect(db,handle,kind,expected_revision)
            if expected_revision==0:raise StorageError('revision_conflict')
            key=handle.scope.learning_context_id
            db.execute("UPDATE task_learning_memory SET state='revoked',quote='',revision=revision+1,updated_at=? "
                       'WHERE context_id=? AND task_id=? AND kind=?',(_now(),key,slot,kind))
            _advance(db,key,slot,_floor(db,key))
