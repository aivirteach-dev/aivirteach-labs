"""有界操作日志：幂等、阶段事件与交付状态；不是另一个聊天记忆库。

单文件单写进程，线程使用短连接。重启把非终态标为待核实，不恢复推理。
响应副本仅用于可靠交付，读取时仍由服务核对ChatStore权威记录。
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import json,os,sqlite3,time
from .service_contracts import ServiceError,ServiceSettings

APP_ID=0x4453484A
TERMINAL={'succeeded','failed'}


def now():return datetime.now(timezone.utc).isoformat()


from .operation_retention import RetentionMixin


class OperationJournal(RetentionMixin):
    def __init__(self,path,settings=None):
        self.path=Path(path).absolute();self.settings=ServiceSettings.model_validate(settings or {})
        self._owner=None;self._closed=False
        if self.path.is_symlink() or not self.path.is_file():raise ServiceError('journal_unavailable',503)
        import fcntl
        fd=os.open(str(self.path)+'.owner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        owner=os.fdopen(fd,'a')
        try:
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB);self._owner=owner
            with self.connection():pass
        except BlockingIOError:
            owner.close();self._owner=None;raise ServiceError('journal_owned',503) from None
        except BaseException:owner.close();self._owner=None;raise

    @classmethod
    def create(cls,path,settings=None):
        """只显式新建，缺失日志不可在启动时静默重建以绕过旧幂等记录。"""
        path=Path(path).absolute()
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        try:
            db=sqlite3.connect(path)
            try:
                db.executescript(f'''PRAGMA application_id={APP_ID}; PRAGMA user_version=1;
                CREATE TABLE operations(
                 operation_id TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, scope_ref TEXT NOT NULL,
                 request_id TEXT NOT NULL, digest TEXT NOT NULL, profile TEXT NOT NULL,
                 instance_id TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
                 cancel_requested INTEGER NOT NULL DEFAULT 0, deadline_exceeded INTEGER NOT NULL DEFAULT 0, error_code TEXT,
                 event_sequence INTEGER NOT NULL DEFAULT 0, result_json TEXT,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                 UNIQUE(owner_ref,request_id));
                CREATE INDEX operation_states ON operations(state);
                CREATE TABLE operation_events(
                 operation_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
                 stage TEXT NOT NULL, code TEXT, created_at TEXT NOT NULL,
                 PRIMARY KEY(operation_id,sequence));''')
                db.commit()
            finally:db.close()
            return cls(path,settings)
        except BaseException:
            # 仅移除本次独占创建且尚未交付的空库，不删除已存在数据库。
            path.unlink(missing_ok=True);raise

    @contextmanager
    def connection(self,*,write=False):
        if self._closed:raise ServiceError('service_closed',503)
        db=None;deadline=time.monotonic()+self.settings.database_timeout_seconds
        try:
            if self.path.is_symlink() or not self.path.is_file():raise ServiceError('journal_unavailable',503)
            db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,isolation_level=None,
                               timeout=self.settings.database_timeout_seconds)
            db.row_factory=sqlite3.Row
            db.set_progress_handler(lambda:int(time.monotonic()>deadline),200)
            if db.execute('PRAGMA application_id').fetchone()[0]!=APP_ID or db.execute('PRAGMA user_version').fetchone()[0] not in (1,2):
                raise ServiceError('unsupported_journal',503)
            page_size=db.execute('PRAGMA page_size').fetchone()[0]
            ceiling=self.settings.max_journal_bytes//page_size
            if db.execute('PRAGMA page_count').fetchone()[0]>ceiling:raise ServiceError('journal_capacity',503)
            db.execute(f'PRAGMA max_page_count={ceiling}')
            db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA cache_size=-512')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None and db.in_transaction:db.rollback()
            code='journal_capacity' if 'full' in str(exc).lower() else 'journal_unavailable'
            raise ServiceError(code,503) from None
        except BaseException:
            if db is not None and db.in_transaction:db.rollback()
            raise
        finally:
            if db is not None:db.close()

    def _event(self,db,op,kind,stage,code=None):
        row=db.execute('SELECT event_sequence FROM operations WHERE operation_id=?',(op,)).fetchone()
        sequence=row[0]+1;stamp=now()
        db.execute('UPDATE operations SET event_sequence=?,updated_at=? WHERE operation_id=?',(sequence,stamp,op))
        db.execute('INSERT INTO operation_events VALUES(?,?,?,?,?,?)',(op,sequence,kind,stage,code,stamp))
        db.execute('DELETE FROM operation_events WHERE operation_id=? AND sequence<=?',
                   (op,sequence-self.settings.events_per_operation))

    def begin_session(self,instance):
        """无论上一轮在哪一步退出，遗留操作只进入人工核实状态，不创建模型任务。"""
        with self.connection(write=True) as db:
            rows=db.execute("SELECT operation_id FROM operations WHERE state NOT IN ('succeeded','failed','recovery_required') LIMIT ?",(self.settings.max_operations+1,)).fetchall()
            if len(rows)>self.settings.max_operations:raise ServiceError('journal_capacity',503)
            for r in rows:
                db.execute("UPDATE operations SET state='recovery_required',error_code='interrupted',instance_id=? WHERE operation_id=?",(instance,r[0]))
                self._event(db,r[0],'interrupted','recovery','interrupted')

    def find(self,owner_ref,request_id):
        with self.connection() as db:
            row=db.execute('SELECT * FROM operations WHERE owner_ref=? AND request_id=?',(owner_ref,request_id)).fetchone()
            return dict(row) if row else None

    def get(self,operation_id,owner_ref):
        with self.connection() as db:
            row=db.execute('SELECT * FROM operations WHERE operation_id=? AND owner_ref=?',(operation_id,owner_ref)).fetchone()
            if row is None:raise ServiceError('not_found',404)
            return dict(row)

    def claim(self,*,operation_id,owner_ref,scope_ref,request_id,digest,profile,instance,window=None):
        with self.connection(write=True) as db:
            old=db.execute('SELECT * FROM operations WHERE owner_ref=? AND request_id=?',(owner_ref,request_id)).fetchone()
            if old:
                if old['digest']!=digest:raise ServiceError('request_conflict')
                return dict(old),False
            if db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]>=self.settings.max_operations:
                raise ServiceError('journal_capacity',503)
            stamp=now()
            db.execute('INSERT INTO operations(operation_id,owner_ref,scope_ref,request_id,digest,profile,instance_id,state,stage,created_at,updated_at) '
                "VALUES(?,?,?,?,?,?,?,'queued','queued',?,?)",(operation_id,owner_ref,scope_ref,request_id,digest,profile,instance,stamp,stamp))
            self._register_window(db,window,owner_ref,request_id,operation_id)
            self._event(db,operation_id,'accepted','queued')
            return dict(db.execute('SELECT * FROM operations WHERE operation_id=?',(operation_id,)).fetchone()),True

    def progress(self,operation_id,instance,stage):
        with self.connection(write=True) as db:
            row=db.execute('SELECT state,instance_id,stage FROM operations WHERE operation_id=?',(operation_id,)).fetchone()
            if row is None or row['instance_id']!=instance or row['state'] in TERMINAL|{'recovery_required'}:return
            if row['stage']==stage:return # 相同进度不重复写事件；关键状态仍FULL同步。
            db.execute("UPDATE operations SET state='running',stage=? WHERE operation_id=?",(stage,operation_id))
            self._event(db,operation_id,'stage',stage)

    def cancel(self,operation_id,owner_ref,code='cancelled'):
        with self.connection(write=True) as db:
            row=db.execute('SELECT state,cancel_requested,stage FROM operations WHERE operation_id=? AND owner_ref=?',(operation_id,owner_ref)).fetchone()
            if row is None:raise ServiceError('not_found',404)
            if row['state'] not in TERMINAL and code=='operation_deadline':
                db.execute('UPDATE operations SET deadline_exceeded=1 WHERE operation_id=?',(operation_id,))
            if row['state'] not in TERMINAL and not row['cancel_requested']:
                db.execute('UPDATE operations SET cancel_requested=1 WHERE operation_id=?',(operation_id,))
                self._event(db,operation_id,'cancel_requested',row['stage'],code)

    def finish(self,operation_id,instance,*,state,result=None,code=None,reconcile=False):
        if state not in TERMINAL|{'recovery_required'}:raise ValueError('invalid_operation_state')
        encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')) if result is not None else None
        if encoded is not None and len(encoded.encode())>self.settings.max_result_bytes:raise ServiceError('result_too_large',503)
        with self.connection(write=True) as db:
            row=db.execute('SELECT * FROM operations WHERE operation_id=?',(operation_id,)).fetchone()
            if row is None:raise ServiceError('not_found',404)
            if row['state'] in TERMINAL:return
            if not reconcile and row['instance_id']!=instance:raise ServiceError('stale_instance')
            db.execute('UPDATE operations SET state=?,stage=?,error_code=?,result_json=?,instance_id=? WHERE operation_id=?',
                       (state,'done' if state in TERMINAL else 'recovery',code,encoded,instance,operation_id))
            self._event(db,operation_id,'reconciled' if reconcile else state,'done' if state in TERMINAL else 'recovery',code)

    def redact(self,operation_id):
        """源聊天撤销/删除后不返回缓存正文；逻辑清空不是安全擦盘或备份清除。"""
        with self.connection(write=True) as db:
            db.execute("UPDATE operations SET state='failed',stage='done',result_json=NULL,error_code='source_changed' WHERE operation_id=?",(operation_id,))
            self._event(db,operation_id,'redacted','done','source_changed')

    def events(self,operation_id,after):
        if type(after) is not int or not 0<=after<2**63:raise ServiceError('invalid_cursor',400)
        with self.connection() as db:
            last=db.execute('SELECT event_sequence FROM operations WHERE operation_id=?',(operation_id,)).fetchone()[0]
            if after>last:raise ServiceError('invalid_cursor',400)
            floor=max(0,last-self.settings.events_per_operation)
            rows=db.execute('SELECT sequence,kind,stage,code,created_at FROM operation_events WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                            (operation_id,max(after,floor),self.settings.events_per_operation)).fetchall()
            return {'items':[dict(r) for r in rows],'next_after':last,'reset_required':after<floor}

    def counts(self):
        with self.connection() as db:
            return {r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM operations GROUP BY state').fetchall()}

    def close(self):
        self._closed=True
        if self._owner is not None:self._owner.close();self._owner=None
