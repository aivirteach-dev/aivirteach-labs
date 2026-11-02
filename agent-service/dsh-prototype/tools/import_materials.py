"""可信开发入口：离线导入带lesson-*模板的多语言教材，不执行网页。"""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from dsh_tutor.materials import import_bilingual


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--course-id',required=True)
    parser.add_argument('--source',type=Path,action='append',required=True)
    parser.add_argument('--root',type=Path,default=ROOT/'materials/courses')
    args=parser.parse_args()
    manifest=import_bilingual(args.source,args.root,args.course_id)
    print(json.dumps({'course_id':manifest['course_id'],'version':manifest['version'],
                      'locales':list(manifest['locales']),'units':len(manifest['lesson_ids'])},ensure_ascii=False))


if __name__=='__main__':
    main()
