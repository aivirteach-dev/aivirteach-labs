"""本机维护协调器：轻量、独立预算、忙时立即延后；没有定时器/任意路径HTTP。"""
from pathlib import Path
import shutil
from datetime import datetime,timezone
from .service_contracts import ServiceError
from .resources import AdmissionError
from .run_registry import RunInventory

class ServiceMaintenance:
    def __init__(self,service,*,runtime_root,chat_path,local_maintenance=None,cleanup_limits=None):
        self.service=service;self.runs=RunInventory(runtime_root,cleanup_limits);self.chat_path=Path(chat_path).absolute()
        self.local_maintenance=local_maintenance;self.last={'state':'not_run'};self._snapshot=None
    def snapshot(self):
        components={};complete=True
        for label,base in (('chat',self.chat_path),('operations',self.service.journal.path)):
            for suffix,tail in (('database',''),('wal','-wal'),('shm','-shm')):
                p=Path(str(base)+tail)
                try:
                    if p.is_symlink():raise ValueError()
                    components[label+'_'+suffix]=p.stat().st_size if p.exists() else 0 if tail else None
                except (OSError,ValueError):components[label+'_'+suffix]=None
        inventory=self.runs.preview();components['runtime_eligible_lower_bound']=inventory['known_eligible_bytes']
        components['runtime_total']=None # 活动/未知/旧目录未完整盘点，不能伪装为0。
        for name in ('backups','validations'):components[name]=None
        if self.local_maintenance is not None:
            try:
                files=self.local_maintenance.capacity()['bytes']
                for name in ('backups','validations'):components[name]=files.get(name)
            except Exception:pass
        known=sum(x for k,x in components.items() if type(x) is int and x>=0)
        free=shutil.disk_usage(self.runs.root).free
        result={'state':'available','collected_at':datetime.now(timezone.utc).isoformat(),'components':components,'known_bytes_lower_bound':known,'coverage_complete':False,
          'free_bytes':free,'pressure':'low_free_space' if free<self.service.tutor.gate.policy.min_free_bytes else 'partial_inventory',
          'runtime_preview':inventory,'last_job':dict(self.last),'automatic_cleanup':False,
          'note':'只计已知路径及可清理运行下界；备份未接入和运行总量保持未知，非完整磁盘预测。'}
        self._snapshot=result;return result
    def status(self):
        if self._snapshot is None:return {'state':'not_collected'}
        return {k:v for k,v in self._snapshot.items() if k!='runtime_preview'}|{'last_job':dict(self.last)}
    def run_once(self,kind,*,preview=True,expected=None,items=None,retention_limits=None):
        if kind not in ('retention','run_cleanup','snapshot'):raise ValueError('unsupported_maintenance_job')
        try:
            with self.service.maintenance():
                if kind=='retention':result=self.service.journal.retention(preview=preview,limits=retention_limits,expected=expected)
                elif kind=='run_cleanup':result=self.runs.preview() if preview else self.runs.apply(items)
                else:result=self.snapshot()
        except (ServiceError,AdmissionError) as exc:
            if exc.code=='maintenance_busy':self.last={'state':'deferred','kind':kind};return dict(self.last)
            self.last={'state':'failed','kind':kind,'code':exc.code};raise
        except Exception:
            self.last={'state':'failed','kind':kind,'code':'maintenance_failed'};raise
        self.last={'state':'finished','kind':kind,'preview':preview};return result
