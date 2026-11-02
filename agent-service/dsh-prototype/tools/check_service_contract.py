"""离线校验接入配置/导出契约：不启动Agent，不创建库，不连接模型。"""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_tutor.service_contracts import API_VERSION,Submission,OperationView,ServiceSettings,load_service_settings


def schema():
    return {'api_version':API_VERSION,'submission':Submission.model_json_schema(),
        'operation_view':OperationView.model_json_schema(),'settings':ServiceSettings.model_json_schema()}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config/service.example.json')
    parser.add_argument('--schema-out',type=Path)
    args=parser.parse_args();settings=load_service_settings(args.config)
    if args.schema_out:
        # 仅在调用者明确指定时写出；不自动覆盖任意系统/平台文件。
        with args.schema_out.open('x',encoding='utf-8') as stream:json.dump(schema(),stream,ensure_ascii=False,indent=2)
    print(json.dumps({'api_version':API_VERSION,'config':'valid','profile':settings.profile,'model_calls':0},ensure_ascii=False))
