"""管理员只读视图：固定数据库、白名单字段、有界采集；不读取聊天正文。

本服务不是学生API。必须由受保护的管理传输层调用；队列只能注入本进程实际
ResourceGate.snapshot，不能用另建空gate冒充问答进程。没有隐式模型探测或重试。
"""
from pathlib import Path
from datetime import datetime, timezone
import json
import sqlite3
import threading
import time
import re


def now():
    return datetime.now(timezone.utc).isoformat()


class ManagementError(Exception):
    pass


class ManagementView:
    def __init__(self, database, config, *, gate=None, scheduler=None, operations=None, material_cache=None, maintenance=None, source='local', ttl=5):
        self.database=Path(database).absolute(); self.config=Path(config).absolute()
        if gate is not None and scheduler is not None:raise ValueError('multiple_queue_sources')
        self.gate=gate; self.scheduler=scheduler;self.source=source; self.ttl=ttl
        self.operations=operations
        self.material_cache=material_cache;self.maintenance=maintenance
        self._lock=threading.Lock(); self._cache=None; self._deadline=0

    def _database_snapshot(self):
        p=self.database
        if p.is_symlink() or p.resolve()!=p or not p.is_file():
            raise ManagementError('database_unavailable')
        # 只读连接；总查询步数有限，锁等待100ms；不运行迁移或写数据库。
        db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=.1)
        db.row_factory=sqlite3.Row
        deadline=time.monotonic()+.5
        db.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        try:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (4,5):raise ManagementError('unsupported_schema')
            rows=db.execute('SELECT sequence,request_ref,state,stage,error_code,elapsed_seconds,timeline_json,created_at '
                            'FROM diagnostic_events ORDER BY sequence DESC LIMIT 200').fetchall()
            events=[]
            from .diagnostics import STAGES
            stages=STAGES
            states={'saved','replayed','in_progress','failed','not_saved','scope_changed'}
            for row in rows:
                # 数据库内容也不完全可信；拒绝未知枚举，绝不直接透传异常文字。
                if row['stage'] not in stages or row['state'] not in states:continue
                ref=row['request_ref']
                if not re.fullmatch('[0-9a-f]{24}',ref):continue
                timeline=json.loads(row['timeline_json'])
                if not isinstance(timeline,list):continue
                clean=[]
                for item in timeline[:64]:
                    if item.get('stage') in stages and type(item.get('seconds')) in (int,float) and 0<=item['seconds']<=86400:
                        clean.append({'stage':item['stage'],'seconds':item['seconds']})
                code=row['error_code']
                from .diagnostics import ERRORS
                code=code if code in ERRORS else 'unclassified' if code else None
                events.append(dict(sequence=row['sequence'],request_ref=ref,state=row['state'],stage=row['stage'],
                    error_code=code,elapsed_seconds=max(0,min(float(row['elapsed_seconds']),86400)),timeline=clean,
                    created_at=datetime.fromisoformat(row['created_at']).isoformat()))
            sizes={name:(Path(str(p)+suffix).stat().st_size if Path(str(p)+suffix).is_file() and not Path(str(p)+suffix).is_symlink() else 0)
                   for name,suffix in [('database',''),('wal','-wal'),('shm','-shm')]}
            return {'state':'available','schema':version,'bytes':sizes,'events':events}
        finally:db.close()

    def snapshot(self):
        """多管理员共享一个有界快照；并发采集不叠加，失败不伪造零值。"""
        if time.monotonic()<self._deadline and self._cache:return self._cache
        if not self._lock.acquire(blocking=False):
            if self._cache:return dict(self._cache,stale=True)
            raise ManagementError('collector_busy')
        try:
            if time.monotonic()<self._deadline and self._cache:return self._cache
            result={'collected_at':now(),'source':self.source,'stale':False,'model':{'state':'unknown','note':'未进行模型探测'},
                'agent':{'state':'unknown','note':'管理服务在线不代表推理进程在线'},
                'tools':{'state':'not_connected','note':'真实Gateway未接入'},'queue':{'state':'not_connected'},
                'backups':{'state':'not_connected'},'events':[]}
            try:
                d=self._database_snapshot();result['storage']={k:v for k,v in d.items() if k!='events'};result['events']=d['events']
            except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,ManagementError):
                result['storage']={'state':'unavailable','error_code':'database_unavailable'}
            self._cache=result;self._deadline=time.monotonic()+self.ttl
            return result
        finally:self._lock.release()

    def query(self, params):
        allowed={'before','limit','state','request_ref','since','until'}
        if set(params)-allowed:raise ManagementError('invalid_query')
        try:
            limit=int(params.get('limit','20'));before=int(params.get('before',str(2**63-1)))
            if not 1<=limit<=50 or not 1<=before<2**63:raise ValueError()
            state=params.get('state','');ref=params.get('request_ref','')
            if state not in ('','saved','replayed','in_progress','failed','not_saved','scope_changed'):raise ValueError()
            if ref and not re.fullmatch('[0-9a-f]{24}',ref):raise ValueError()
            lower=datetime.fromisoformat(params['since'].replace('Z','+00:00')) if params.get('since') else None
            upper=datetime.fromisoformat(params['until'].replace('Z','+00:00')) if params.get('until') else None
            if any(x and not x.tzinfo for x in (lower,upper)):raise ValueError()
            if lower and upper and lower>upper:raise ValueError()
        except (TypeError,ValueError):raise ManagementError('invalid_query') from None
        snap=self.snapshot()
        if snap['storage']['state']!='available':raise ManagementError('database_unavailable')
        found=[r for r in snap['events'] if r['sequence']<before and (not state or r['state']==state)
               and (not ref or r['request_ref']==ref) and (not lower or datetime.fromisoformat(r['created_at'])>=lower)
               and (not upper or datetime.fromisoformat(r['created_at'])<=upper)]
        return {'items':found[:limit],'next_before':found[limit-1]['sequence'] if len(found)>limit else None,
                'collected_at':snap['collected_at'],'source':self.source,'stale':snap['stale'],
                'retention_note':'仅最近200条可用诊断；不是完整请求审计；请求编号为哈希引用。'}

    def overview(self):
        s=self.snapshot();result={k:v for k,v in s.items() if k!='events'}
        # 数据库仍共用5秒缓存；调度独立采集，避免短等待在旧快照中消失。
        from .scheduler_status import queue_projection
        try:
            if self.gate is not None:
                result['queue']={'state':'available',**queue_projection(self.gate()),
                                 'source':self.source,'collected_at':now(),'scope':'本进程实际问答实例'}
            elif self.scheduler is not None:result['queue']=self.scheduler()
        except Exception:result['queue']={'state':'unknown','reason':'status_unavailable'}
        # 只接实际服务聚合计数，不返回操作编号、聊天内容、学生或令牌。
        result['maintenance']={'state':'not_connected'}
        if self.maintenance is not None:
            try:
                raw=self.maintenance()
                if raw['state']!='available':result['maintenance']={'state':'not_collected'}
                else:
                    keys={'chat_database','chat_wal','chat_shm','operations_database','operations_wal','operations_shm','runtime_eligible_lower_bound','runtime_total','backups','validations'}
                    components={k:raw['components'][k] for k in keys}
                    numeric={k:raw[k] for k in ('known_bytes_lower_bound','free_bytes')}
                    if any(v is not None and (type(v) is not int or not 0<=v<2**63) for v in list(components.values())+list(numeric.values())):raise ValueError('invalid_capacity')
                    result['maintenance']={'state':'available','components':components,**numeric,'coverage_complete':False,
                        'collected_at':str(raw['collected_at'])[:64],'pressure':raw['pressure'] if raw['pressure'] in ('low_free_space','partial_inventory') else 'unknown',
                        'automatic_cleanup':False,'note':'手动采集缓存，页面刷新不会扫描或删除目录。'}
            except Exception:result['maintenance']={'state':'unknown'}
        result['material_cache']={'state':'not_connected'}
        if self.material_cache is not None:
            try:
                raw=self.material_cache();numbers={k:raw[k] for k in ('entries','bytes','hits','misses','evictions')}
                if any(type(v) is not int or not 0<=v<2**63 for v in numbers.values()):raise ValueError('invalid_cache_metrics')
                from .material_publication import MaterialCacheLimits
                limits=MaterialCacheLimits.model_validate(raw['limits'])
                result['material_cache']={'state':'available',**numbers,'limits':limits.model_dump(),'scope':'公共不可变课时，不含学生记忆'}
            except Exception:result['material_cache']={'state':'unknown'}
        result['operations']={'state':'not_connected'}
        if self.operations is not None:
            try:
                raw=self.operations();counts=raw['counts']
                safe={k:counts[k] for k in ('queued','running','succeeded','failed','recovery_required') if k in counts}
                numeric={k:raw[k] for k in ('active_operations','max_inflight','delivery_issues')}
                if any(type(x) is not int or x<0 for x in [*safe.values(),*numeric.values()]):raise ValueError()
                if type(raw['ready']) is not bool or type(raw['closing']) is not bool:raise ValueError()
                result['operations']={'state':'available',**numeric,'counts':safe,'ready':raw['ready'],'closing':raw['closing'],
                    'source':self.source,'collected_at':now(),'usage':'unknown','scope':'本进程实际操作日志'}
                ownership=raw.get('model_ownership')
                if ownership is not None:
                    expected={'scope','active','capacity','quarantined','control_busy','runtime_policy'}
                    if set(ownership)!=expected or ownership['scope']!='single_process' or ownership['runtime_policy']!='per_request_isolated':raise ValueError()
                    if type(ownership['active']) is not int or type(ownership['capacity']) is not int or not (0<=ownership['active']<=64 and 1<=ownership['capacity']<=64):raise ValueError()
                    if any(type(ownership[k]) is not bool for k in ('quarantined','control_busy')):raise ValueError()
                    result['operations']['model_ownership']=ownership
                from .scheduler_status import _Stats
                timings=raw.get('timings',{})
                if set(timings)-{'delivery_save','operation_total'}:raise ValueError()
                direct={k:raw[k] for k in ('direct_saved','direct_tool_calls') if k in raw}
                if any(type(v) is not int or not 0<=v<2**63 for v in direct.values()):raise ValueError('invalid_direct_metrics')
                result['operations'].update(direct)
                result['operations']['timings']={k:_Stats.model_validate(v).model_dump() for k,v in timings.items()}
            except Exception:result['operations']={'state':'unknown','reason':'status_unavailable'}
        grouped={}
        for e in s['events']:
            if e['error_code']:grouped[e['error_code']]=grouped.get(e['error_code'],0)+1
        result['errors']=[{'code':k,'count':v} for k,v in sorted(grouped.items(),key=lambda x:-x[1])]
        result['sample_count']=len(s['events']) if s['storage']['state']=='available' else None
        return result

    def configuration(self):
        """只公开业务预算；模型地址、令牌、内部路径不出接口。"""
        try:
            data=json.loads(self.config.read_text())
            keys=('model','context_window','max_output_tokens','total_timeout_seconds','sdk_request_timeout_seconds')
            safe={k:data[k] for k in keys if k in data}
            for v in safe.values():
                if type(v) not in (str,int,float) or isinstance(v,str) and len(v)>128:raise ValueError()
            from .execution_budget import ExecutionLimits
            limits=ExecutionLimits.model_validate(data.get('execution_limits',{})).model_dump()
            from .providers import provider_for
            try:provider=provider_for(data).public()
            except (ValueError,KeyError):provider={'state':'invalid_configuration'}
            return {'provider':provider,'runtime_policy':'per_request_isolated','lifecycle':'explicit_host_control_only; no_network_probe','state':'available','values':safe,'execution_limits':limits,'read_only':True,'model_health':'unknown','actual_usage':'unknown'}
        except (OSError,ValueError):return {'state':'unavailable','read_only':True}
