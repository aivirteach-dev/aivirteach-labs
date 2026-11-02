"""管理员本地维护适配：容量快照、一致性备份、隔离恢复验证。

路径来自可信应用配置，不暴露为学生HTTP命令。无自动清理/定时器/模型调用。
副本和清单应放在受限目录；校验值检查完整性，不替代文件权限或异机灾备。
"""
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field
from .storage import StorageError,APPLICATION_ID

VALIDATION_ID=0x44534856


class MaintenanceLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_backup_files:int=Field(default=5,ge=1,le=100)
    max_validation_runs:int=Field(default=3,ge=1,le=100)
    max_backup_bytes:int=Field(default=64*1024*1024,ge=65536,le=1024*1024*1024)
    min_free_bytes:int=Field(default=256*1024*1024,ge=0)
    deadline_seconds:float=Field(default=30.0,gt=0,le=300)
    pages_per_step:int=Field(default=64,ge=1,le=256)


def _digest(path):
    result=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(65536),b''):result.update(block)
    return result.hexdigest()


def _at_limit(folder,limit):
    """达到上限即停止枚举，不把异常大目录完整加载进内存。"""
    return any(i+1>=limit for i,_ in enumerate(folder.iterdir()))


class LocalMaintenance:
    """一个配置目录共用排他锁，失败不自动抢占遗留锁；管理员确认后处理。"""
    def __init__(self,store,root,limits=None):
        self.store=store;self.root=Path(root).absolute()
        self.limits=MaintenanceLimits.model_validate({} if limits is None else limits)
        if self.root.is_symlink():raise StorageError('unsafe_maintenance_path')
        if self.root.resolve()!=self.root:raise StorageError('unsafe_maintenance_path')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        for name in ('backups','validations'):
            folder=self.root/name
            if folder.is_symlink():raise StorageError('unsafe_maintenance_path')
            folder.mkdir(exist_ok=True,mode=0o700)

    @contextmanager
    def _job(self):
        lock=self.root/'.maintenance.lock'
        try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:raise StorageError('maintenance_busy') from None
        try:
            identity=os.fstat(fd).st_ino;os.close(fd)
            deadline=time.monotonic()+self.limits.deadline_seconds
            yield deadline
        except (sqlite3.Error,OSError):
            raise StorageError('maintenance_timeout' if time.monotonic()>deadline else 'maintenance_failed') from None
        finally:
            if lock.exists() and not lock.is_symlink() and lock.stat().st_ino==identity:lock.unlink()

    def _space(self,required):
        if shutil.disk_usage(self.root).free<self.limits.min_free_bytes+required:raise StorageError('disk_low')

    def _progress(self,deadline,page_size):
        def step(status,remaining,total):
            if time.monotonic()>deadline:raise StorageError('maintenance_timeout')
            if total*page_size>self.limits.max_backup_bytes:raise StorageError('backup_capacity')
        return step

    @contextmanager
    def _connect_read(self,path):
        if path.is_symlink() or not path.is_file():raise StorageError('backup_unavailable')
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0.1)
        try:yield db
        finally:db.close()

    def _validate(self,db,deadline,*,expected_id=APPLICATION_ID):
        if time.monotonic()>deadline:raise StorageError('maintenance_timeout')
        db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        if db.execute('PRAGMA application_id').fetchone()[0]!=expected_id or db.execute('PRAGMA user_version').fetchone()[0] not in (1,2,3,4,5):
            raise StorageError('unsupported_schema')
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise StorageError('backup_corrupt')
        stats={'schema_version':db.execute('PRAGMA user_version').fetchone()[0],
                'contexts':db.execute('SELECT COUNT(*) FROM contexts').fetchone()[0],
                'turns':db.execute('SELECT COUNT(*) FROM turns').fetchone()[0],
                'pending':db.execute("SELECT COUNT(*) FROM turns WHERE status='pending'").fetchone()[0]}
        if stats['schema_version']==5:stats['inspection_records']=db.execute('SELECT COUNT(*) FROM inspection_records').fetchone()[0]
        if time.monotonic()>deadline:raise StorageError('maintenance_timeout')
        return stats

    def capacity(self):
        """管理员按需快照，只看已配置位置，不扫描项目/学生正文，不猜增长趋势。"""
        files={}
        for name,path in [('database',self.store.path),('journal',Path(str(self.store.path)+'-journal')),
                          ('wal',Path(str(self.store.path)+'-wal')),('shm',Path(str(self.store.path)+'-shm'))]:
            if path.is_symlink():raise StorageError('unsafe_maintenance_path')
            files[name]=path.stat().st_size if path.exists() else 0
        for name in ('backups','validations'):
            # 仅固定层级，目录异常或超限明确返回错误，不无限递归。
            count=0;size=0
            for item in (self.root/name).iterdir():
                count+=1
                if count>1000 or item.is_symlink():raise StorageError('maintenance_inventory_limit')
                if item.is_file():size+=item.stat().st_size
                elif name=='validations' and item.is_dir():
                    for n,child in enumerate(item.iterdir()):
                        if n>10 or child.is_symlink() or not child.is_file():raise StorageError('maintenance_inventory_limit')
                        size+=child.stat().st_size
                else:raise StorageError('maintenance_inventory_limit')
            files[name]=size
        with self.store._connection() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            counts={'contexts':db.execute('SELECT COUNT(*) FROM contexts').fetchone()[0],
                    'turns':db.execute('SELECT COUNT(*) FROM turns').fetchone()[0]}
            if version>=3:counts['tasks']=db.execute('SELECT COUNT(*) FROM learning_tasks').fetchone()[0]
            if version>=4:
                counts['archived_tasks']=db.execute('SELECT COUNT(*) FROM learning_tasks WHERE archived=1').fetchone()[0]
                counts['diagnostic_events']=db.execute('SELECT COUNT(*) FROM diagnostic_events').fetchone()[0]
            if version==5:counts['inspection_records']=db.execute('SELECT COUNT(*) FROM inspection_records').fetchone()[0]
        return {'at':datetime.now(timezone.utc).isoformat(),'bytes':files,'counts':counts,
                'database_volume_free_bytes':shutil.disk_usage(self.store.path.parent).free,
                'maintenance_volume_free_bytes':shutil.disk_usage(self.root).free,
                'growth_rate':None,'automatic_actions':False,'note':'当前大小快照；归档不释放磁盘，非全盘统计或容量预测。'}

    def create_backup(self):
        with self._job() as deadline:
            folder=self.root/'backups'
            if _at_limit(folder,self.limits.max_backup_files*2):raise StorageError('backup_count_limit')
            self._space(self.limits.max_backup_bytes)
            backup_id=uuid4().hex;path=folder/(backup_id+'.sqlite');manifest=folder/(backup_id+'.json')
            created_path=created_manifest=False
            try:
                with self._connect_read(self.store.path) as source:
                    pages=source.execute('PRAGMA page_count').fetchone()[0];page_size=source.execute('PRAGMA page_size').fetchone()[0]
                    if pages*page_size>self.limits.max_backup_bytes:raise StorageError('backup_capacity')
                    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
                    created_path=True
                    target=sqlite3.connect(path,timeout=0.1)
                    try:source.backup(target,pages=self.limits.pages_per_step,progress=self._progress(deadline,page_size),sleep=0.01)
                    finally:target.close()
                if path.stat().st_size>self.limits.max_backup_bytes:raise StorageError('backup_capacity')
                with self._connect_read(path) as db:stats=self._validate(db,deadline)
                result={'backup_id':backup_id,'created_at':datetime.now(timezone.utc).isoformat(),'sha256':_digest(path),
                        'bytes':path.stat().st_size,'stats':stats,'consistent_snapshot':True,
                        'latest_revocations_reconciled':False,'scope':'本地整库快照，非异机灾备；不可交给普通学生。'}
                if time.monotonic()>deadline:raise StorageError('maintenance_timeout')
                with manifest.open('x',encoding='utf-8') as f:
                    created_manifest=True
                    json.dump(result,f,ensure_ascii=False,indent=2)
                os.chmod(manifest,0o600)
                return result
            except BaseException:
                # 只清理本次新建的未交付文件；绝不清理已有备份、聊天或来源库。
                if created_path:path.unlink(missing_ok=True)
                if created_manifest:manifest.unlink(missing_ok=True)
                raise

    def validate_restore(self,backup_id):
        """恢复只生成隔离副本，改用验证专属application_id，普通ChatStore必定拒绝。"""
        if type(backup_id) is not str or not re.fullmatch('[0-9a-f]{32}',backup_id):raise StorageError('invalid_input')
        with self._job() as deadline:
            folder=self.root/'backups';path=folder/(backup_id+'.sqlite');manifest=folder/(backup_id+'.json')
            if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size>16384:raise StorageError('backup_unavailable')
            try:record=json.loads(manifest.read_text(encoding='utf-8'))
            except (ValueError,UnicodeError):raise StorageError('backup_corrupt') from None
            if not isinstance(record,dict):raise StorageError('backup_corrupt')
            if path.is_symlink() or not path.is_file():raise StorageError('backup_unavailable')
            if path.stat().st_size>self.limits.max_backup_bytes:raise StorageError('backup_capacity')
            if record.get('backup_id')!=backup_id or _digest(path)!=record.get('sha256'):raise StorageError('backup_corrupt')
            if _at_limit(self.root/'validations',self.limits.max_validation_runs):raise StorageError('validation_count_limit')
            self._space(path.stat().st_size)
            run_id=uuid4().hex;destination=self.root/'validations'/run_id;destination.mkdir(mode=0o700)
            (destination/'.validation-only').write_text('独立恢复验证；禁止普通问答。',encoding='utf-8')
            target_path=destination/'snapshot.sqlite'
            try:
                with self._connect_read(path) as source:
                    source_stats=self._validate(source,deadline)
                    target=sqlite3.connect(target_path,timeout=0.1)
                    try:
                        page_size=source.execute('PRAGMA page_size').fetchone()[0]
                        source.backup(target,pages=self.limits.pages_per_step,progress=self._progress(deadline,page_size),sleep=0.01)
                        target.execute(f'PRAGMA application_id={VALIDATION_ID}');target.commit()
                        restored_stats=self._validate(target,deadline,expected_id=VALIDATION_ID)
                    finally:target.close()
                os.chmod(target_path,0o600)
                if source_stats!=restored_stats or _digest(path)!=record['sha256']:raise StorageError('backup_corrupt')
                if time.monotonic()>deadline:raise StorageError('maintenance_timeout')
                report={'validation_id':run_id,'backup_id':backup_id,'backup_created_at':record.get('created_at'),
                        'restored_stats':restored_stats,'status':'validation_only','chat_enabled':False,
                        'live_database_replaced':False,'latest_revocations_reconciled':False,
                        'note':'只验证快照能恢复。可能包含备份后的旧授权/旧撤销状态；未对账，不得用于问答；pending不自动重试。'}
                (destination/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                os.chmod(destination/'report.json',0o600)
                return report
            except BaseException:
                # 已创建的验证目录保留失败线索，不覆盖原库或删除其他副本。
                raise
