"""只读检查私有教材来源和提取资料；不下载、不调用模型、不执行教材内容。"""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsh_tutor.materials import MaterialRepository


def check(root=ROOT):
    report = json.loads((root/'development/materials-import-20261029.json').read_text(encoding='utf-8'))
    sources = root/'inputs/courses'/report['course_id']/'originals'
    for name, expected in report['source_sha256'].items():
        path = sources/name
        if path.is_symlink() or not path.is_file():
            raise ValueError('private_materials_missing')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('private_materials_source_mismatch')
    repo = MaterialRepository(root/'materials/courses')
    for locale in report['languages']:
        repo.load(report['course_id'], report['version'], locale)
    return {'status': 'passed', 'course_id': report['course_id'], 'version': report['version'],
            'model_calls': 0, 'writes': 0}


if __name__ == '__main__':
    try:
        result = check()
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'failed', 'error': 'private_materials_missing_or_invalid',
                          'next': '见README私有教材章节；不自动下载或跳过验收。'}, ensure_ascii=False))
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False))
