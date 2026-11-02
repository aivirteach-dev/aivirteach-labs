"""显式操作日志v2保留：答案副本先过期，可信有效期后才移除去重凭据。"""
import hashlib,json,time
from datetime import datetime,timezone
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .service_contracts import ServiceError

class RequestWindow(BaseModel):
    """仅可信认证宿主创建；学生提交的批次ID不是签名或授权。"""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    nonce:str=Field(pattern=r'^[0-9a-f]{32}$')
    issued_at:int=Field(ge=0)
    expires_at:int=Field(gt=0)
    @model_validator(mode='after')
    def valid(self):
        if not 1<=self.expires_at-self.issued_at<=86400:raise ValueError('invalid_request_window')
        return self
    def identifier(self,owner):
        # 时间参与不可变ID，新的有效期不能复用已回收的旧请求前缀。
        return hashlib.sha256(json.dumps([owner,self.nonce,self.issued_at,self.expires_at],separators=(',',':')).encode()).hexdigest()

class RetentionLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_operations:int=Field(default=100,ge=1,le=100)
    result_seconds:float=Field(default=86400.0,ge=1,le=31536000)
    expiry_grace_seconds:float=Field(default=3600.0,ge=0,le=31536000)

class RetentionMixin:
    def enable_retention(self):
        """可信控制面显式v1→v2迁移，禁止遗留在途；旧软件拒绝v2而非绕过去重。"""
        with self.connection(write=True) as db:
            if db.execute('PRAGMA user_version').fetchone()[0]==2:return
            if db.execute("SELECT 1 FROM operations WHERE state NOT IN ('succeeded','failed') LIMIT 1").fetchone():raise ServiceError('reconciliation_required',503)
            for statement in (
              'CREATE TABLE request_windows(window_id TEXT PRIMARY KEY,owner_ref TEXT NOT NULL,issued_at INTEGER NOT NULL,expires_at INTEGER NOT NULL)',
              'CREATE TABLE operation_windows(operation_id TEXT PRIMARY KEY,window_id TEXT NOT NULL)',
              'CREATE INDEX operation_windows_by_window ON operation_windows(window_id)',
              'CREATE TABLE retention_clock(id INTEGER PRIMARY KEY CHECK(id=1),minimum_time REAL NOT NULL)',
              'INSERT INTO retention_clock VALUES(1,0)',
              'PRAGMA user_version=2'):db.execute(statement)
    def _window_check(self,db,window,owner,request_id,presented=None):
        if db.execute('PRAGMA user_version').fetchone()[0]!=2:raise ServiceError('retention_migration_required',503)
        window=RequestWindow.model_validate(window.model_dump());identifier=window.identifier(owner);at=time.time()
        floor=db.execute('SELECT minimum_time FROM retention_clock WHERE id=1').fetchone()[0]
        if at<floor:raise ServiceError('clock_verification_required',503)
        if window.issued_at<self.settings.request_issued_floor:raise ServiceError('request_expired',410)
        if not window.issued_at<=at<window.expires_at:raise ServiceError('request_expired',410)
        if (presented is not None and presented!=identifier) or not request_id.startswith(identifier+'.'):raise ServiceError('invalid_request_window',422)
        return identifier
    def validate_window(self,window,owner,request_id,presented):
        with self.connection() as db:return self._window_check(db,window,owner,request_id,presented)
    def _register_window(self,db,window,owner,request_id,operation_id):
        if window is None:return
        identifier=self._window_check(db,window,owner,request_id)
        old=db.execute('SELECT owner_ref,issued_at,expires_at FROM request_windows WHERE window_id=?',(identifier,)).fetchone()
        expected=(owner,window.issued_at,window.expires_at)
        if old is not None and tuple(old)!=expected:raise ServiceError('invalid_request_window',422)
        db.execute('INSERT OR IGNORE INTO request_windows VALUES(?,?,?,?)',(identifier,*expected))
        db.execute('INSERT INTO operation_windows VALUES(?,?)',(operation_id,identifier))
    def retention(self,*,preview=True,limits=None,expected=None):
        """单次最多100操作；非终态保留。预览是CAS凭据，不接受学生时间或目录。"""
        limits=RetentionLimits.model_validate(limits or {});at=time.time();cutoff=datetime.fromtimestamp(at-limits.result_seconds,timezone.utc).isoformat()
        grace_cutoff=datetime.fromtimestamp(at-limits.expiry_grace_seconds,timezone.utc).isoformat()
        with self.connection(write=not preview) as db:
            if db.execute('PRAGMA user_version').fetchone()[0]!=2:raise ServiceError('retention_migration_required',503)
            floor=db.execute('SELECT minimum_time FROM retention_clock WHERE id=1').fetchone()[0]
            if at<floor:raise ServiceError('clock_verification_required',503)
            rows=db.execute("SELECT o.operation_id,o.state,o.event_sequence,o.updated_at,o.result_json,w.expires_at FROM operations o LEFT JOIN operation_windows x ON x.operation_id=o.operation_id LEFT JOIN request_windows w ON w.window_id=x.window_id WHERE o.state IN ('succeeded','failed') AND ((w.expires_at<=? AND o.updated_at<=?) OR (o.result_json IS NOT NULL AND o.updated_at<=?)) ORDER BY o.created_at,o.operation_id LIMIT ?",(at-limits.expiry_grace_seconds,grace_cutoff,cutoff,limits.max_operations)).fetchall()
            selected=[{'operation_id':r['operation_id'],'action':'remove_expired_window' if r['expires_at'] is not None and r['expires_at']+limits.expiry_grace_seconds<=at and r['updated_at']<=grace_cutoff else 'expire_result_copy'} for r in rows]
            digest=hashlib.sha256(json.dumps([selected,[[r['operation_id'],r['state'],r['event_sequence'],r['updated_at'],hashlib.sha256((r['result_json'] or '').encode()).hexdigest(),r['expires_at']] for r in rows]],separators=(',',':')).encode()).hexdigest()
            if not preview:
                if expected!=digest:raise ServiceError('maintenance_preview_changed')
                for row in selected:
                    op=row['operation_id']
                    if row['action']=='remove_expired_window':
                        db.execute('DELETE FROM operation_events WHERE operation_id=?',(op,));db.execute('DELETE FROM operation_windows WHERE operation_id=?',(op,));db.execute('DELETE FROM operations WHERE operation_id=?',(op,))
                    else:
                        db.execute("UPDATE operations SET result_json=NULL,state='failed',stage='done',error_code='result_expired' WHERE operation_id=?",(op,))
                        self._event(db,op,'redacted','done','result_expired')
                db.execute('DELETE FROM request_windows WHERE expires_at<=? AND window_id NOT IN (SELECT window_id FROM operation_windows)',(at,))
                db.execute('UPDATE retention_clock SET minimum_time=? WHERE id=1',(at,))
            return {'preview':preview,'selected':selected,'token':digest,'limit':limits.max_operations,'bounded':True,'rows':len(rows),'note':'只清理操作副本/有可信有效期的凭据；不删除聊天、未完成记录，不执行VACUUM。'}
