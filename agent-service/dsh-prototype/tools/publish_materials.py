"""离线预处理明确教材版本；不请求模型、不执行教材命令、不切换发布版本。"""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_tutor.materials import MaterialRepository
from dsh_tutor.material_publication import publish_materials
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]/'materials/courses'))
    parser.add_argument('--course',required=True);parser.add_argument('--version',required=True)
    args=parser.parse_args();print(json.dumps(publish_materials(MaterialRepository(args.root),args.course,args.version),ensure_ascii=False))
