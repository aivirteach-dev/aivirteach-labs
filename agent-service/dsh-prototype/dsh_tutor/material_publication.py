"""发布时预处理不可变教材；请求只读课时。有界缓存从不保存学生上下文。"""
from pathlib import Path
from collections import OrderedDict
from threading import RLock
import hashlib,json,os,shutil,tempfile
from pydantic import BaseModel,ConfigDict,Field

PREPARER='material-preparation-v1'
MAX_LESSON_BYTES=512*1024
MAX_FILE_BYTES=8*1024*1024

def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()

def read_bounded(path,maximum=MAX_FILE_BYTES):
    with Path(path).open('rb') as stream:data=stream.read(maximum+1)
    if len(data)>maximum:raise ValueError('material_file_too_large')
    return data

class MaterialCacheLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_entries:int=Field(default=64,ge=0,le=256)
    max_bytes:int=Field(default=4*1024*1024,ge=0,le=32*1024*1024)

class LessonCache:
    """只保存序列化的公共课时，按LRU淘汰；返回新对象，不共享可变列表。"""
    def __init__(self,limits=None):
        self.limits=MaterialCacheLimits.model_validate(limits or {});self._lock=RLock();self._rows=OrderedDict();self._bytes=0
        self._hits=0;self._misses=0;self._evictions=0
    def get(self,key):
        with self._lock:
            value=self._rows.pop(key,None)
            if value is None:self._misses+=1;return None
            self._rows[key]=value;self._hits+=1;return json.loads(value)
    def put(self,key,value):
        raw=encoded(value)
        with self._lock:
            prior=self._rows.pop(key,None)
            if prior:self._bytes-=len(prior)
            if not self.limits.max_entries or len(raw)>self.limits.max_bytes:return
            while self._rows and (len(self._rows)>=self.limits.max_entries or self._bytes+len(raw)>self.limits.max_bytes):
                _,removed=self._rows.popitem(last=False);self._bytes-=len(removed);self._evictions+=1
            self._rows[key]=raw;self._bytes+=len(raw)
    def snapshot(self):
        with self._lock:return dict(entries=len(self._rows),bytes=self._bytes,hits=self._hits,misses=self._misses,evictions=self._evictions,limits=self.limits.model_dump(),scope='public_immutable_lessons_only')

def compile_lesson(document,lesson):
    from .materials import _pieces,_terms
    pieces=[p for block in lesson['blocks'] for p in _pieces(block)]
    # 发布时建轻量字词索引；有界词典不影响完整原文保存，未收录词仍可直接匹配。
    index={}
    for i,piece in enumerate(pieces):
        for term in sorted(_terms(piece['text'])):
            if term not in index and len(index)>=4096:continue
            index.setdefault(term,[]).append(i)
    result={'preparer':PREPARER,'course_id':document['course_id'],'version':document['version'],'locale':document['locale'],
      'source_filename':document['source_filename'],'source_sha256':document['source_sha256'],
      'lesson_id':lesson['lesson_id'],'title':lesson['title'],'blocks':lesson['blocks'],'pieces':pieces,'index':index,
      'goals':[b['text'] for b in lesson['blocks'] if b.get('role')=='goal'][:12],
      'goals_declared':any(b.get('role')=='goal' for b in lesson['blocks'])}
    if len(encoded(result))>MAX_LESSON_BYTES:raise ValueError('prepared_lesson_too_large')
    return result

def publish_materials(repository,course_id,version):
    """构建到同盘暂存目录，全部验证后单次rename；已有版本不可覆盖。"""
    from .materials import _inside,_key
    course_id,version=map(_key,(course_id,version));base=_inside(repository.root,course_id,version)
    manifest=json.loads(read_bounded(base/'manifest.json'));documents=[repository.load(course_id,version,locale) for locale in sorted(manifest['locales'])]
    destination=_inside(repository.root,course_id,version,PREPARER);stage=Path(tempfile.mkdtemp(prefix='.prepare-',dir=base))
    try:
        files={}
        for document in documents:
            folder=stage/document['locale'];folder.mkdir()
            for lesson in document['lessons']:
                name=document['locale']+'/'+_key(lesson['lesson_id'])+'.json';raw=encoded(compile_lesson(document,lesson));(stage/name).write_bytes(raw)
                files[name]=hashlib.sha256(raw).hexdigest()
        publication={'preparer':PREPARER,'course_id':course_id,'version':version,'source_manifest_sha256':hashlib.sha256(read_bounded(base/'manifest.json')).hexdigest(),'files':files}
        raw=encoded(publication);(stage/'publication.json').write_bytes(raw)
        # 验证字节完整；正式读取时再次核对文件指纹，不信任半成品。
        for name,digest in files.items():
            if hashlib.sha256(read_bounded(stage/name,MAX_LESSON_BYTES)).hexdigest()!=digest:raise ValueError('publication_validation_failed')
        if destination.exists():
            if read_bounded(destination/'publication.json')!=raw:raise ValueError('immutable_publication_conflict')
            for name,digest in files.items():
                if hashlib.sha256(read_bounded(destination/name,MAX_LESSON_BYTES)).hexdigest()!=digest:raise ValueError('immutable_publication_conflict')
        else:
            stage.chmod(0o755);os.rename(stage,destination)
        # 公共教材需要被受限运行账号读取；暂存目录的700不能带入已发布目录。
        destination.chmod(0o755)
        for name in files:
            (destination/name).parent.chmod(0o755);(destination/name).chmod(0o644)
        (destination/'publication.json').chmod(0o644)
        return {'status':'published','preparer':PREPARER,'course_id':course_id,'version':version,'lessons':len(files),'sha256':hashlib.sha256(raw).hexdigest(),'model_calls':0}
    finally:
        if stage.exists():shutil.rmtree(stage) # 仅本函数创建并持有的暂存目录，不触及已发布目录。
