"""离线维护：显式配置/预览/CAS/有限批次；服务运行或待恢复时拒绝，不创建库。"""
from pathlib import Path
import argparse,json,sys,os
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pydantic import BaseModel,ConfigDict
from dsh_tutor.operation_journal import OperationJournal
from dsh_tutor.run_registry import RunInventory,CleanupLimits
from dsh_tutor.operation_retention import RetentionLimits
class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    journal:str
    runtime_root:str
    cleanup:CleanupLimits=CleanupLimits()
    retention:RetentionLimits=RetentionLimits()
def inside(value):
    raw=Path(value)
    if raw.is_absolute() or '..' in raw.parts:raise ValueError('maintenance_path_outside_project')
    result=(ROOT/raw).resolve()
    if not result.is_relative_to(ROOT):raise ValueError('maintenance_path_outside_project')
    return result
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config/service-maintenance.example.json')
    parser.add_argument('--action',required=True,choices=['migrate-retention','preview-retention','apply-retention','preview-runs','apply-runs','snapshot'])
    parser.add_argument('--plan');parser.add_argument('--out');args=parser.parse_args()
    path=inside(args.config)
    if path.stat().st_size>16384:raise ValueError('maintenance_config_too_large')
    config=Settings.model_validate(json.loads(path.read_text()));j=OperationJournal(inside(config.journal))
    try:
        if any(j.counts().get(k,0) for k in ('queued','running','recovery_required')):raise ValueError('reconciliation_required')
        inventory=RunInventory(inside(config.runtime_root),config.cleanup.model_dump());plan=None
        if args.plan:
            path=inside(args.plan)
            if path.stat().st_size>65536:raise ValueError('maintenance_plan_too_large')
            plan=json.loads(path.read_text())
        if args.action=='migrate-retention':j.enable_retention();result={'journal_schema':2,'model_calls':0}
        elif args.action=='preview-retention':result=j.retention(limits=config.retention.model_dump())
        elif args.action=='apply-retention':
            if not isinstance(plan,dict):raise ValueError('preview_required')
            result=j.retention(preview=False,limits=config.retention.model_dump(),expected=plan.get('token'))
        elif args.action=='preview-runs':result=inventory.preview()
        elif args.action=='apply-runs':
            if not isinstance(plan,dict):raise ValueError('preview_required')
            result=inventory.apply(plan.get('items'))
        else:result={'operations_database_bytes':j.path.stat().st_size,'runtime':inventory.preview(),'complete_inventory':False,'model_calls':0}
        if args.out:
            output=inside(args.out);fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f:json.dump(result,f,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False))
    finally:j.close()
if __name__=='__main__':main()
