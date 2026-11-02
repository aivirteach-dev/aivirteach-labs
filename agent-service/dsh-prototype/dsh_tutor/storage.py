"""聊天持久化契约及SQLite适配器；仅可信应用调用，不自动接入DSH或保存真实学生数据。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from .budgets import storage_deadline
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .conversation import ConversationScope


class StorageError(RuntimeError):
    """稳定原因码供应用定位；不输出路径、SQL、学生身份或原始消息。"""
    def __init__(self, code):
        self.code = code
        super().__init__('storage:' + code)


class StorageLimits(BaseModel):
    """可信配置；限制查询、锁等待和数据量，不宣称是整个磁盘的硬配额。"""
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    busy_timeout_ms: int = Field(default=1000, ge=1, le=10000)
    max_db_bytes: int = Field(default=64*1024*1024, ge=65536, le=1024*1024*1024)
    max_turns_per_context: int = Field(default=10000, ge=1, le=1000000)
    max_contexts: int = Field(default=1000, ge=1, le=100000)
    max_inspection_bytes: int = Field(default=32768, ge=1024, le=65536)
    page_limit: int = Field(default=20, ge=1, le=100)


class ScopeHandle(BaseModel):
    """应用绑定的范围及存储修订号；不是可由学生自报的登录凭据。"""
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    scope: ConversationScope
    revision: int = Field(ge=1)


class StoredTurn(BaseModel):
    """完整保存问题和最终回答；partial指回答质量，不能与存储状态混淆。"""
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    sequence: int
    request_id: str
    scope_revision: int
    question: str
    status: Literal['pending', 'completed', 'failed', 'cancelled']
    answer: str | None
    answer_status: Literal['completed', 'partial'] | None
    run_id: str | None
    error_code: str | None
    created_at: str
    updated_at: str
    history_excluded: bool = False


class TurnPage(BaseModel):
    """按序号向前翻页；返回页内升序，不提供无限量全文读取。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    turns: list[StoredTurn]
    next_before: int | None


class AcceptedTurn(BaseModel):
    """原子接受结果；只有created为true的调用者可以发起本次模型任务。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    turn: StoredTurn
    created: bool


class ChatStore(Protocol):
    """未来平台适配器沿用的行为接口，不要求复用SQLite表结构。"""
    @property
    def page_capacity(self) -> int: ...
    def accept(self, handle: ScopeHandle, request_id: str, question: str) -> AcceptedTurn: ...
    def activate(self, scope: ConversationScope, *, expected_revision: int = 0) -> ScopeHandle: ...
    def current(self, *, student_id: str, learning_context_id: str) -> ScopeHandle: ...
    def begin(self, handle: ScopeHandle, request_id: str, question: str) -> StoredTurn: ...
    def finish(self, handle: ScopeHandle, request_id: str, *, answer: str | None = None,
               answer_status: str = 'completed', error_code: str | None = None,
               run_id: str | None = None, expected_memory_epoch: int | None = None, inspection: object | None = None) -> StoredTurn: ...
    def check_pending(self, handle: ScopeHandle, request_id: str, sequence: int, question: str) -> None: ...
    def get_request(self, handle: ScopeHandle, request_id: str) -> StoredTurn | None: ...
    def read_page(self, handle: ScopeHandle, *, before: int | None = None, limit: int | None = None) -> TurnPage: ...
    def delete_history(self, handle: ScopeHandle) -> int: ...


def _text(value, maximum):
    if type(value) is not str or not value.strip() or len(value) > maximum or '\x00' in value:
        raise StorageError('invalid_input')
    return value


def _scope(value):
    try:
        result = ConversationScope.model_validate(value.model_dump() if isinstance(value, ConversationScope) else value)
    except (ValidationError, AttributeError, TypeError):
        raise StorageError('invalid_scope') from None
    for field in ['student_id','learning_context_id','course_id','course_version','lesson_id']:
        _text(getattr(result, field),128)
    if (result.instance_id is None) != (result.instance_generation is None):
        raise StorageError('invalid_scope')
    for field in ['instance_id','instance_generation']:
        if getattr(result,field) is not None:
            _text(getattr(result,field),128)
    return result


def _handle(value):
    try:
        handle = ScopeHandle.model_validate(value.model_dump() if isinstance(value,ScopeHandle) else value)
    except (ValidationError, AttributeError, TypeError):
        raise StorageError('invalid_scope') from None
    _scope(handle.scope)
    return handle


def _encoded(scope):
    return json.dumps(scope.model_dump(),ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE contexts (
 context_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0)
);
CREATE TABLE scopes (
 context_id TEXT NOT NULL REFERENCES contexts(context_id), revision INTEGER NOT NULL,
 scope_json TEXT NOT NULL, PRIMARY KEY(context_id,revision)
);
CREATE TABLE turns (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 context_id TEXT NOT NULL, scope_revision INTEGER NOT NULL, request_id TEXT NOT NULL,
 question TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','cancelled')),
 answer TEXT, answer_status TEXT CHECK(answer_status IN ('completed','partial')),
 error_code TEXT, run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(context_id,request_id),
 FOREIGN KEY(context_id,scope_revision) REFERENCES scopes(context_id,revision),
 CHECK((status='completed' AND answer IS NOT NULL AND answer_status IS NOT NULL AND error_code IS NULL)
    OR (status='pending' AND answer IS NULL AND answer_status IS NULL AND error_code IS NULL)
    OR (status IN ('failed','cancelled') AND answer IS NULL AND answer_status IS NULL AND error_code IS NOT NULL))
);
CREATE INDEX turns_scope_order ON turns(context_id,scope_revision,sequence);
CREATE UNIQUE INDEX one_pending_per_context ON turns(context_id) WHERE status='pending';
"""
APPLICATION_ID = 0x44534843


class SQLiteChatStore:
    """本地单机适配器；每次操作短连接/显式事务，不常驻聊天缓存或后台任务。

    数据库路径和传入身份必须来自可信应用。此模块隔离查询，不提供登录认证或
    OS沙箱；持有整个数据库文件权限的程序仍能读取文件。生产经Server授权接入。
    """
    def __init__(self, path, *, limits=None):
        self.path = Path(path).absolute()
        self.limits = StorageLimits.model_validate(
            limits.model_dump() if isinstance(limits,StorageLimits) else ({} if limits is None else limits))
        if self.path.is_symlink() or not self.path.is_file():
            raise StorageError('unavailable')
        with self._connection():
            pass

    @classmethod
    def create(cls, path, *, limits=None):
        """显式新建，绝不覆盖已有文件；父目录由可信应用准备并限制访问。"""
        path = Path(path).absolute()
        try:
            fd = os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:
            raise StorageError('already_exists') from None
        except OSError:
            raise StorageError('unavailable') from None
        os.close(fd)
        store = object.__new__(cls)
        store.path = path
        try:
            store.limits = StorageLimits.model_validate(
                limits.model_dump() if isinstance(limits,StorageLimits) else ({} if limits is None else limits))
            with store._connection(validate=False) as db:
                db.execute('PRAGMA journal_mode=DELETE')
                db.executescript('BEGIN IMMEDIATE;'+SCHEMA+
                    f'PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1; COMMIT;')
        except Exception:
            # 只移除本次O_EXCL创建且尚未交付的失败新文件，不删除已有数据库。
            path.unlink(missing_ok=True)
            raise
        return store

    def migrate_learning_memory(self):
        """可信应用显式将v1升级到v2；单事务，不覆盖原聊天，不复制活动数据库。"""
        from .learning_memory import MEMORY_SCHEMA
        with self._connection(write=True) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==1:
                for statement in MEMORY_SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
                db.execute('PRAGMA user_version=2')

    @staticmethod
    def _memory_schema(db):
        if db.execute('PRAGMA user_version').fetchone()[0]!=2:
            raise StorageError('task_binding_required' if db.execute('PRAGMA user_version').fetchone()[0] in (3,4,5) else 'memory_migration_required')

    def remember_question(self,handle,request_id,question,*,limits):
        """同一当前范围内原子更正；仅引用已提交的学生原问题，旧助手内容不参与。"""
        from .learning_memory import explicit_statements,MemoryLimits
        handle=_handle(handle);_text(request_id,128)
        limits=MemoryLimits.model_validate(limits.model_dump())
        try:
            statements=explicit_statements(question,limits)
        except ValueError:
            raise StorageError('invalid_memory_statement') from None
        with self._connection(write=True) as db:
            self._memory_schema(db);self._check(db,handle,active=True)
            key=handle.scope.learning_context_id
            source=db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone()
            if (source is None or source['scope_revision']!=handle.revision or source['question']!=question
                    or source['status']!='pending'):
                raise StorageError('memory_source_rejected')
            for kind,start,end,value in statements:
                # 偏好仅在本学习记录跨实例保留，不跨学生/学习记录；其他类型严格限当前修订。
                slot=0 if kind=='preference' else handle.revision
                old=db.execute('SELECT revision FROM learning_memory WHERE context_id=? AND slot_scope=? AND kind=?',
                               (key,slot,kind)).fetchone()
                revision=old[0]+1 if old else 1
                db.execute('INSERT INTO learning_memory VALUES(?,?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(context_id,slot_scope,kind) DO UPDATE SET revision=excluded.revision,active=1,'
                           'source_request_id=excluded.source_request_id,source_start=excluded.source_start,'
                           'source_end=excluded.source_end,quote=excluded.quote',
                           (key,slot,kind,revision,1,request_id,start,end,value))
            if statements:
                self._advance_memory(db,key,source['sequence'])
        return self.learning_items(handle)

    @staticmethod
    def _advance_memory(db,key,floor):
        db.execute('INSERT INTO learning_memory_state VALUES(?,?,1) ON CONFLICT(context_id) '
                   'DO UPDATE SET history_floor=MAX(history_floor,excluded.history_floor),epoch=epoch+1',(key,floor))

    def learning_state(self,handle):
        """返回对话使用下界和单调修订号；避免更正/删除后从旧助手回答复活记忆。"""
        handle=_handle(handle)
        with self._connection() as db:
            self._memory_schema(db);self._check(db,handle,active=True)
            row=db.execute('SELECT history_floor,epoch FROM learning_memory_state WHERE context_id=?',
                           (handle.scope.learning_context_id,)).fetchone()
            return (row[0],row[1]) if row else (0,0)

    def learning_items(self,handle):
        """最多四项；同一快照内验证范围、来源和摘录，不扫描全部聊天。"""
        from .learning_memory import LearningItem
        handle=_handle(handle)
        with self._connection() as db:
            self._memory_schema(db);self._check(db,handle,active=True)
            rows=db.execute('SELECT m.*,t.question,t.error_code FROM learning_memory m JOIN turns t '
                            'ON t.context_id=m.context_id AND t.request_id=m.source_request_id '
                            'WHERE m.context_id=? AND m.slot_scope IN (0,?) AND m.active=1 ORDER BY m.kind LIMIT 5',
                            (handle.scope.learning_context_id,handle.revision)).fetchall()
            if len(rows)>4:
                raise StorageError('invalid_memory')
            result=[]
            for row in rows:
                if (row['error_code']=='history_deleted' or not row['quote']
                        or row['question'][row['source_start']:row['source_end']]!=row['quote']):
                    raise StorageError('memory_source_rejected')
                result.append(LearningItem(kind=row['kind'],revision=row['revision'],scope_revision=row['slot_scope'],
                    source_request_id=row['source_request_id'],source_start=row['source_start'],
                    source_end=row['source_end'],text=row['quote']))
            return result

    def forget_learning(self,handle,kind,*,expected_revision):
        """显式撤销，比较版本防止旧界面删除新更正；不修改原聊天。"""
        from .learning_memory import PREFIXES
        handle=_handle(handle)
        if kind not in PREFIXES.values() or type(expected_revision) is not int or expected_revision<1:
            raise StorageError('invalid_input')
        slot=0 if kind=='preference' else handle.revision
        with self._connection(write=True) as db:
            self._memory_schema(db);self._check(db,handle,active=True)
            row=db.execute('SELECT revision FROM learning_memory WHERE context_id=? AND slot_scope=? AND kind=?',
                           (handle.scope.learning_context_id,slot,kind)).fetchone()
            if row is None or row[0]!=expected_revision:
                raise StorageError('revision_conflict')
            db.execute("UPDATE learning_memory SET active=0,quote='',revision=revision+1 WHERE context_id=? AND slot_scope=? AND kind=?",
                       (handle.scope.learning_context_id,slot,kind))
            floor=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM turns WHERE context_id=?',
                             (handle.scope.learning_context_id,)).fetchone()[0]
            self._advance_memory(db,handle.scope.learning_context_id,floor)

    def learning_brief(self,handle):
        """按需原文摘录摘要；从有效记录即时生成，纠正/删除后无需清理重复摘要缓存。"""
        from .learning_memory import LearningBrief
        items=self.learning_items(handle)
        labels={'goal':'学生目标','attempt':'学生表示已尝试','open_issue':'学生尚未解决的问题','preference':'学生明确偏好'}
        return LearningBrief(text='\n'.join(labels[x.kind]+'：'+x.text for x in items),items=items)


    @contextmanager
    def _connection(self, *, write=False, validate=True):
        db = None
        try:
            if (self.path.parent/'.validation-only').exists():
                raise StorageError('validation_only')
            if self.path.is_symlink() or not self.path.is_file():
                raise StorageError('unavailable')
            deadline=storage_deadline.get()
            remaining=deadline-time.monotonic() if deadline is not None else self.limits.busy_timeout_ms/1000
            if remaining<=0:raise StorageError('timeout')
            db = sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,
                                 isolation_level=None,timeout=min(self.limits.busy_timeout_ms/1000,remaining))
            if deadline is not None:
                db.set_progress_handler(lambda:int(time.monotonic()>=deadline),200)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA cache_size=-1024')
            size = db.execute('PRAGMA page_size').fetchone()[0]
            ceiling = self.limits.max_db_bytes//size
            if db.execute('PRAGMA page_count').fetchone()[0] > ceiling:
                raise StorageError('capacity')
            db.execute(f'PRAGMA max_page_count={ceiling}')
            if validate and (db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID
                             or db.execute('PRAGMA user_version').fetchone()[0] not in (1,2,3,4,5)):
                raise StorageError('unsupported_schema')
            # 读操作也用短快照，保证归属检查和记录读取处于同一事务。
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            if db.in_transaction:
                db.commit()
        except sqlite3.Error as exc:
            if db is not None and db.in_transaction:
                db.rollback()
            message = str(exc).lower()
            code = ('timeout' if storage_deadline.get() is not None and time.monotonic()>=storage_deadline.get() else
                    'busy' if 'locked' in message or 'busy' in message else
                    'capacity' if 'full' in message else
                    'corrupt' if 'malformed' in message or 'not a database' in message else 'unavailable')
            raise StorageError(code) from None
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _owner(self, db, student_id, context_id):
        row = db.execute('SELECT * FROM contexts WHERE context_id=?',(context_id,)).fetchone()
        if row is None or row['student_id']!=student_id:
            raise StorageError('scope_denied')
        return row

    def _check(self, db, handle, *, active=False):
        context = self._owner(db,handle.scope.student_id,handle.scope.learning_context_id)
        row = db.execute('SELECT scope_json FROM scopes WHERE context_id=? AND revision=?',
                         (handle.scope.learning_context_id,handle.revision)).fetchone()
        if row is None or row['scope_json']!=_encoded(handle.scope):
            raise StorageError('scope_denied')
        if active and context['revision']!=handle.revision:
            raise StorageError('stale_scope')

    @staticmethod
    def _turn(row):
        if 'inspection_body' in row.keys() and not row['history_excluded']:
            from .inspection_memory import check_history_marker
            check_history_marker(row)
        return StoredTurn.model_validate({key:bool(row[key]) if key=='history_excluded' else row[key] for key in StoredTurn.model_fields if key in row.keys()})

    def current(self, *, student_id, learning_context_id):
        _text(student_id,128); _text(learning_context_id,128)
        with self._connection() as db:
            context=self._owner(db,student_id,learning_context_id)
            row=db.execute('SELECT scope_json FROM scopes WHERE context_id=? AND revision=?',
                           (learning_context_id,context['revision'])).fetchone()
            return ScopeHandle(scope=_scope(json.loads(row['scope_json'])),revision=context['revision'])

    def activate(self, scope, *, expected_revision=0):
        """可信应用切换范围；比较修订号，取消旧pending，不允许他人抢占学习记录。"""
        scope=_scope(scope)
        if type(expected_revision) is not int or expected_revision<0:
            raise StorageError('invalid_input')
        key=scope.learning_context_id
        with self._connection(write=True) as db:
            row=db.execute('SELECT * FROM contexts WHERE context_id=?',(key,)).fetchone()
            if row is None:
                if expected_revision!=0:
                    raise StorageError('revision_conflict')
                if db.execute('SELECT COUNT(*) FROM contexts').fetchone()[0]>=self.limits.max_contexts:
                    raise StorageError('capacity')
                revision=1
                db.execute('INSERT INTO contexts VALUES(?,?,?)',(key,scope.student_id,revision))
            else:
                self._owner(db,scope.student_id,key)
                if row['revision']!=expected_revision:
                    raise StorageError('revision_conflict')
                old=db.execute('SELECT scope_json FROM scopes WHERE context_id=? AND revision=?',
                               (key,row['revision'])).fetchone()[0]
                if old==_encoded(scope):
                    return ScopeHandle(scope=scope,revision=row['revision'])
                revision=row['revision']+1
                db.execute('UPDATE contexts SET revision=? WHERE context_id=?',(revision,key))
                db.execute("UPDATE turns SET status='cancelled',error_code='scope_changed',updated_at=? "
                           "WHERE context_id=? AND status='pending'",(_now(),key))
            db.execute('INSERT INTO scopes VALUES(?,?,?)',(key,revision,_encoded(scope)))
            return ScopeHandle(scope=scope,revision=revision)

    @property
    def inspection_required(self):
        with self._connection() as db:return db.execute('PRAGMA user_version').fetchone()[0]==5

    @property
    def page_capacity(self):
        """向应用公开分页能力，不要求它读取SQLite内部配置。"""
        return self.limits.page_limit

    def begin(self, handle, request_id, question):
        """第一步已有调用保留；唯一执行权须使用accept的created判断。"""
        return self.accept(handle,request_id,question).turn

    def accept(self, handle, request_id, question):
        """原子操作；任务适配器复用同一内核，不复制状态机。"""
        with self._connection(write=True) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] in (3,4,5):
                raise StorageError('task_binding_required')
            return self._accept_in(db, handle, request_id, question)

    def _accept_in(self, db, handle, request_id, question):
        """共享事务内核；调用者必须持有本存储的写事务。"""
        handle=_handle(handle); _text(request_id,128); _text(question,2000)
        key=handle.scope.learning_context_id
        self._check(db,handle,active=True)
        row=db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone()
        if row:
            if row['scope_revision']!=handle.revision or row['question']!=question:
                raise StorageError('request_conflict')
            return AcceptedTurn(turn=self._turn(row),created=False)
        if db.execute("SELECT 1 FROM turns WHERE context_id=? AND status='pending'",(key,)).fetchone():
            raise StorageError('request_in_progress')
        if db.execute('SELECT COUNT(*) FROM turns WHERE context_id=?',(key,)).fetchone()[0]>=self.limits.max_turns_per_context:
            raise StorageError('capacity')
        now=_now()
        db.execute('INSERT INTO turns(context_id,scope_revision,request_id,question,status,created_at,updated_at) '
                   "VALUES(?,?,?,?,'pending',?,?)",(key,handle.revision,request_id,question,now,now))
        return AcceptedTurn(turn=self._turn(db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone()),created=True)

    def finish(self, handle, request_id, *, answer=None, answer_status='completed', error_code=None, run_id=None, expected_memory_epoch=None, inspection=None):
        """原子操作；任务适配器复用同一内核，不复制状态机。"""
        with self._connection(write=True) as db:
            return self._finish_in(db, handle, request_id, answer=answer, answer_status=answer_status, error_code=error_code, run_id=run_id, expected_memory_epoch=expected_memory_epoch, inspection=inspection)

    def _finish_in(self, db, handle, request_id, *, answer=None, answer_status='completed', error_code=None, run_id=None, expected_memory_epoch=None, inspection=None):
        """共享事务内核；调用者必须持有本存储的写事务。"""
        handle=_handle(handle); _text(request_id,128)
        if run_id is not None:
            _text(run_id,128)
        if answer is not None:
            _text(answer,16000)
            if error_code is not None or answer_status not in ('completed','partial'):
                raise StorageError('invalid_input')
            status='completed'
        else:
            if error_code not in ('interrupted','model_failed','timeout','cancelled','application_failed'):
                raise StorageError('invalid_input')
            status='failed'; answer_status=None
        key=handle.scope.learning_context_id
        self._check(db,handle,active=True)
        if expected_memory_epoch is not None:
            self._memory_schema(db)
            if type(expected_memory_epoch) is not int or expected_memory_epoch<0:
                raise StorageError('invalid_input')
            state=db.execute('SELECT epoch FROM learning_memory_state WHERE context_id=?',(key,)).fetchone()
            if (state[0] if state else 0)!=expected_memory_epoch:
                raise StorageError('memory_changed')
        row=db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone()
        if row is None or row['scope_revision']!=handle.revision:
            raise StorageError('request_not_found')
        if db.execute('PRAGMA user_version').fetchone()[0]==5 and answer is not None:
            from .inspection_memory import save_in_transaction
            save_in_transaction(db,handle,row,inspection,answer=answer,run_id=run_id,maximum=self.limits.max_inspection_bytes)
        if row['status']!='pending':
            if (row['status'],row['answer'],row['answer_status'],row['error_code'],row['run_id']) != (status,answer,answer_status,error_code,run_id):
                raise StorageError('terminal_conflict')
            return self._turn(row)
        db.execute('UPDATE turns SET status=?,answer=?,answer_status=?,error_code=?,run_id=?,updated_at=? '
                   'WHERE context_id=? AND request_id=?',
                   (status,answer,answer_status,error_code,run_id,_now(),key,request_id))
        return self._turn(db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=?',(key,request_id)).fetchone())

    @staticmethod
    def _pending_matches(row,sequence,question):
        if row is None or row['status']!='pending' or row['sequence']!=sequence or row['question']!=question:
            raise StorageError('stale_request')

    def check_pending(self,handle,request_id,sequence,question):
        """执行前只读核对接受时的记录，绝不通过重新accept制造新的执行权。"""
        handle=_handle(handle);_text(request_id,128);_text(question,2000)
        with self._connection() as db:
            self._check(db,handle,active=True)
            row=db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=?',
                           (handle.scope.learning_context_id,request_id)).fetchone()
            self._pending_matches(row,sequence,question)

    def get_request(self,handle,request_id):
        """只读查询一个原请求，供交付对账；不会接受新问题或赋予模型执行权。"""
        handle=_handle(handle);_text(request_id,128)
        with self._connection() as db:
            self._check(db,handle,active=True)
            row=db.execute('SELECT * FROM turns WHERE context_id=? AND request_id=? AND scope_revision=?',
                (handle.scope.learning_context_id,request_id,handle.revision)).fetchone()
            return self._turn(row) if row else None

    def read_page(self, handle, *, before=None, limit=None):
        """显式范围的记录查看接口，含未完成/失败记录；不是自动构造模型历史。"""
        handle=_handle(handle)
        limit=self.limits.page_limit if limit is None else limit
        if type(limit) is not int or not 1<=limit<=self.limits.page_limit:
            raise StorageError('invalid_input')
        if before is not None and (type(before) is not int or before<1):
            raise StorageError('invalid_input')
        with self._connection() as db:
            self._check(db,handle)
            if db.execute('PRAGMA user_version').fetchone()[0]==5:
                rows=db.execute("SELECT t.*,CASE WHEN r.kind='no_tools' THEN 0 ELSE 1 END AS history_excluded, CASE WHEN r.kind='no_tools' THEN r.body_json ELSE NULL END AS inspection_body,r.body_sha256 AS inspection_hash FROM turns t "
                                'LEFT JOIN inspection_records r ON r.context_id=t.context_id AND r.request_id=t.request_id '
                                "WHERE t.context_id=? AND t.scope_revision=? AND t.sequence<? AND (t.error_code IS NULL OR t.error_code!='history_deleted') ORDER BY t.sequence DESC LIMIT ?",
                                (handle.scope.learning_context_id,handle.revision,before or 9223372036854775807,limit+1)).fetchall()
            else:
                rows=db.execute("SELECT * FROM turns WHERE context_id=? AND scope_revision=? AND sequence<? AND (error_code IS NULL OR error_code!='history_deleted') "
                                'ORDER BY sequence DESC LIMIT ?',
                                (handle.scope.learning_context_id,handle.revision,before or 9223372036854775807,limit+1)).fetchall()
            selected=rows[:limit]
            return TurnPage(turns=[self._turn(row) for row in reversed(selected)],
                            next_before=selected[-1]['sequence'] if len(rows)>limit else None)

    def delete_history(self, handle):
        """可信应用显式清理某一修订范围；保留请求编号墓碑，防止晚到重试复活已删消息。

        墓碑不保留文本，仅用于拒绝重复编号；不是安全擦盘或备份删除。
        """
        handle=_handle(handle)
        with self._connection(write=True) as db:
            self._check(db,handle)
            key=handle.scope.learning_context_id
            count=db.execute("SELECT COUNT(*) FROM turns WHERE context_id=? AND scope_revision=? "
                             "AND (error_code IS NULL OR error_code!='history_deleted')",
                             (key,handle.revision)).fetchone()[0]
            if db.execute('PRAGMA user_version').fetchone()[0] in (2,3,4,5):
                db.execute("UPDATE learning_memory SET active=0,quote='',revision=revision+1 WHERE context_id=? AND source_request_id IN (SELECT request_id FROM turns WHERE context_id=? AND scope_revision=?)",(key,key,handle.revision))
                floor=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM turns WHERE context_id=?',(key,)).fetchone()[0]
                self._advance_memory(db,key,floor)
            if db.execute('PRAGMA user_version').fetchone()[0] in (3,4,5):
                from .task_memory import invalidate_history
                invalidate_history(db,key,handle.revision)
            if db.execute('PRAGMA user_version').fetchone()[0]==5:
                db.execute('DELETE FROM inspection_records WHERE context_id=? AND scope_revision=?',(key,handle.revision))
            db.execute("UPDATE turns SET question='',answer=NULL,answer_status=NULL,run_id=NULL,status='cancelled',"
                       "error_code='history_deleted',updated_at=? WHERE context_id=? AND scope_revision=?",
                       (_now(),key,handle.revision))
            return count
