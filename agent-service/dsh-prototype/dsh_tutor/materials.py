"""教材适配：离线提取HTML课时，精确选择版本/语言，并生成有出处的有限摘录。"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .contracts import CourseSnapshot, MaterialContext

VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
IGNORE_TAGS = {'script', 'style', 'svg', 'nav'}
BLOCK_TAGS = {'h2', 'h3', 'h4', 'p', 'li', 'pre', 'dl', 'table', 'figcaption'}
SAFE_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
EXTRACTOR_REVISION = 'lesson-templates-v3'


@dataclass
class Node:
    """只保留提取需要的属性；不复制base64图片或执行网页脚本。"""
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text())
                if child.tag in {'p', 'li', 'dt', 'dd', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4'}:
                    parts.append('\n')
        return ''.join(parts)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()


class LessonHTMLParser(HTMLParser):
    """识别lesson-*模板；其他来源格式应另写适配，不按标题猜测课时。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.language = ''
        self.skip = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'html':
            self.language = attrs.get('lang', '')
        classes = attrs.get('class', '').split()
        # 复制按钮也可能承载教材正文值；保留inline-copy-link文字，但不执行按钮行为。
        ignored = (tag in IGNORE_TAGS or 'code-bar' in classes
                   or (tag == 'button' and 'inline-copy-link' not in classes))
        if self.skip or ignored:
            if tag not in VOID_TAGS:
                self.skip.append(tag)
            return
        kept = {key: attrs[key] for key in ('id', 'alt') if key in attrs}
        node = Node(tag, kept)
        self.stack[-1].children.append(node)
        if tag == 'br':
            node.children.append('\n')
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:
                reverse_index = self.skip[::-1].index(tag)
                del self.skip[len(self.skip)-reverse_index-1:]
            return
        for index in range(len(self.stack)-1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.skip:
            self.stack[-1].children.append(data)


def extract_html(data: bytes) -> dict:
    """保留全部文字/代码块；图片原样保存在原HTML，但不声称已识别图中内容。"""
    parser = LessonHTMLParser()
    parser.feed(data.decode('utf-8-sig'))
    lessons = []
    for template in parser.root.walk():
        anchor = template.attrs.get('id', '')
        if template.tag != 'template' or not anchor.startswith('lesson-'):
            continue
        titles = [node.text().strip() for node in template.walk() if node.tag == 'h1']
        if len(titles) != 1:
            raise ValueError('课时必须有且仅有一个标题。')
        blocks = []

        goal_section = False
        def collect(node):
            nonlocal goal_section
            if node.tag in {'h2','h3','h4'}:
                goal_section = node.text().strip().lower().rstrip('：:') in {'学习目标','目标','learning objectives','learning goals','objectives'}
            if node.tag in BLOCK_TAGS:
                raw = node.text()
                text = raw.strip('\r\n') if node.tag == 'pre' else re.sub(r'\s+', ' ', raw).strip()
                if text:
                    blocks.append({'id': f'b{len(blocks)+1:03d}',
                                   'kind': 'code' if node.tag == 'pre' else 'text', 'text': text,
                                   'role': 'goal' if goal_section and node.tag in {'p','li'} else 'content'})
                return
            for child in node.children:
                if isinstance(child, Node):
                    collect(child)

        collect(template)
        lessons.append({'lesson_id': anchor, 'title': titles[0], 'blocks': blocks,
                        'image_count': sum(node.tag == 'img' for node in template.walk())})
    if not lessons or len({x['lesson_id'] for x in lessons}) != len(lessons):
        raise ValueError('没有课时模板，或课时ID重复。')
    if not parser.language:
        raise ValueError('教材缺少明确语言。')
    return {'locale': parser.language, 'lessons': lessons}


def _key(value):
    if not isinstance(value, str) or not SAFE_KEY.fullmatch(value) or value in {'.', '..'}:
        raise ValueError('课程、版本、语言或课时标识不合法。')
    return value


def _inside(root: Path, *parts) -> Path:
    """可信配置仍做路径约束；拒绝路径穿越和指向目录外的符号链接。"""
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('教材路径超出指定目录。')
    return path


def _pieces(block, max_chars=700):
    """代码保持完整，长文本按行分片；保留原文范围，不添加不存在的内容。"""
    text = block['text']
    if block['kind']=='code':
        # 安装命令/代码必须完整；过长时由选择器整体省略，不能给学生半条命令。
        yield {'block_id':block['id'],'kind':'code','start':0,'end':len(text),'text':text}
        return
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind('\n', start+max_chars//2, end)
            if boundary > start:
                end = boundary + 1
        yield {'block_id': block['id'], 'kind': block['kind'],
               'start': start, 'end': end, 'text': text[start:end]}
        start = end


def _terms(question):
    """轻量确定性匹配，不引入向量数据库；中文用双字片段，英文用词。"""
    terms = set(re.findall(r'[a-zA-Z0-9_.$-]{2,}', question.lower()))
    terms -= {'the', 'this', 'that', 'does', 'which', 'what', 'how', 'is', 'are', 'do',
              'for', 'and', 'in', 'to', 'of', 'course', 'use', 'it', 'an', 'on'}
    for phrase in re.findall(r'[\u4e00-\u9fff]+', question):
        terms.update(phrase[i:i+2] for i in range(len(phrase)-1))
    return terms


class MaterialRepository:
    """只读、本地精确版本适配器；模型不能通过工具参数指定文件路径。"""
    def __init__(self, root, *, cache_limits=None):
        from .material_publication import LessonCache
        self.root = Path(root).resolve()
        self.cache = LessonCache(cache_limits)

    def lesson(self,course_id,version,locale,lesson_id):
        """缓存键含内容版本/提取器/语言/课时和文件元数据；不混入问题或私人记忆。"""
        from .material_publication import PREPARER,read_bounded,compile_lesson,MAX_LESSON_BYTES
        course_id,version,locale,lesson_id=map(_key,(course_id,version,locale,lesson_id))
        manifest_path=_inside(self.root,course_id,version,'manifest.json')
        raw=read_bounded(manifest_path);manifest=json.loads(raw)
        if (manifest['course_id'],manifest['version'])!=(course_id,version) or locale not in manifest['locales']:raise ValueError('material_identity_mismatch')
        publication=_inside(self.root,course_id,version,PREPARER,'publication.json')
        name=locale+'/'+lesson_id+'.json'
        if publication.exists():
            pub_raw=read_bounded(publication);pub=json.loads(pub_raw)
            if (pub['preparer'],pub['course_id'],pub['version'],pub['source_manifest_sha256'])!=(PREPARER,course_id,version,hashlib.sha256(raw).hexdigest()):raise ValueError('publication_identity_mismatch')
            digest=pub['files'].get(name)
            if digest is None:raise ValueError('lesson_unavailable')
            path=_inside(self.root,course_id,version,PREPARER,locale,lesson_id+'.json')
            stat=path.stat();key=(course_id,version,locale,lesson_id,manifest.get('extractor_revision'),PREPARER,digest,stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            cached=self.cache.get(key)
            if cached is not None:return cached
            data=read_bounded(path,MAX_LESSON_BYTES)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('prepared_fingerprint_mismatch')
            lesson=json.loads(data)
        else:
            # 旧导入版本仍可读；发布新课程后应提前调用publish_materials，避免首问编制索引。
            path=_inside(self.root,course_id,version,locale+'.json');stat=path.stat()
            key=(course_id,version,locale,lesson_id,manifest.get('extractor_revision'),PREPARER,manifest['locales'][locale]['material_sha256'],stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            cached=self.cache.get(key)
            if cached is not None:return cached
            doc=self.load(course_id,version,locale)
            source=next((x for x in doc['lessons'] if x['lesson_id']==lesson_id),None)
            if source is None:raise ValueError('lesson_unavailable')
            lesson=compile_lesson(doc,source)
        if (lesson['course_id'],lesson['version'],lesson['locale'],lesson['lesson_id'],lesson['preparer'])!=(course_id,version,locale,lesson_id,PREPARER):raise ValueError('lesson_identity_mismatch')
        self.cache.put(key,lesson);return lesson

    def block(self,course_id,version,locale,lesson_id,block_id):
        """明确动作只返回完整原文；不给其他课时兜底，不静默截断。"""
        lesson=self.lesson(course_id,version,locale,lesson_id)
        block=next((b for b in lesson['blocks'] if b['id']==block_id),None)
        if block is None:raise ValueError('material_block_unavailable')
        if len(block['text'].encode())>6000:raise ValueError('material_block_too_large')
        return {'text':block['text'],'kind':block['kind'],'source_filename':lesson['source_filename'],'source_sha256':lesson['source_sha256'],'lesson_id':lesson_id,'block_id':block_id}


    def load(self, course_id, version, locale) -> dict:
        course_id, version, locale = map(_key, (course_id, version, locale))
        manifest = json.loads(_inside(self.root, course_id, version, 'manifest.json').read_text())
        if (manifest['course_id'], manifest['version']) != (course_id, version):
            raise ValueError('教材索引身份不匹配。')
        if locale not in manifest['locales']:
            raise ValueError('没有请求语言的教材，不自动回退其他语言。')
        path = _inside(self.root, course_id, version, locale+'.json')
        from .material_publication import read_bounded
        data = read_bounded(path)
        if hashlib.sha256(data).hexdigest() != manifest['locales'][locale]['material_sha256']:
            raise ValueError('教材内容指纹不匹配。')
        document = json.loads(data)
        if (document['course_id'], document['version'], document['locale']) != (course_id, version, locale):
            raise ValueError('教材文档身份不匹配。')
        return document

    def prepare(self, course_id, version, locale, lesson_id, question, *, max_excerpt_chars=1400):
        """仅在显式课时内选摘录；不给其他课时兜底，完整原文不受摘录预算影响。"""
        if type(max_excerpt_chars) is not int or not 700 <= max_excerpt_chars <= 4000:
            raise ValueError('摘录字符预算须在700到4000之间。')
        document = self.lesson(course_id,version,locale,lesson_id)
        lesson = document
        pieces = document['pieces']
        if not pieces:
            raise ValueError('课时没有可提取文字。')
        terms = _terms(question)
        def rank(index):
            text = pieces[index]['text'].lower()
            return (-sum(len(term) for term in terms if index in document['index'].get(term,[]) or term in text), index)
        # 配置值可能只写在相邻代码块中；把说明与代码邻居一起选入，避免只取标签漏值。
        ordered = [0]
        for index in sorted(range(len(pieces)), key=rank):
            ordered.append(index)
            for neighbor in (index-1, index+1):
                if 0 <= neighbor < len(pieces) and (pieces[index]['kind']=='code' or pieces[neighbor]['kind']=='code'):
                    ordered.append(neighbor)
        order = list(dict.fromkeys(ordered))
        chosen, used = [], 0
        for index in order:
            if len(chosen) == 12:
                break
            size = len(pieces[index]['text'])
            if used+size <= max_excerpt_chars:
                chosen.append(index)
                used += size
        selected = [pieces[index] for index in sorted(chosen)]
        refs = [{'block_id': x['block_id'], 'start': x['start'], 'end': x['end']} for x in selected]
        context = MaterialContext(source_filename=document['source_filename'],
                                  source_sha256=document['source_sha256'],
                                  anchor=lesson_id, excerpts=refs,
                                  excerpt_only=len(chosen) != len(pieces),
                                  images_interpreted=False,
                                  omitted_code_blocks=sum(p['kind']=='code' and i not in chosen for i,p in enumerate(pieces)))
        return CourseSnapshot(course_id=course_id, version=version, locale=locale,
                              lesson_id=lesson_id, title=lesson['title'], goals=document['goals'],
                              instructions=[x['text'] for x in selected], material=context)


def import_bilingual(sources: list[Path], root: Path, course_id: str) -> dict:
    """显式导入，不覆盖已有不同内容；v5文件名仅作来源标签，不冒充平台课程版本。"""
    course_id = _key(course_id)
    parsed = []
    for path in sources:
        raw = path.read_bytes()
        parsed.append((path, hashlib.sha256(raw).hexdigest(), extract_html(raw)))
    locales = [item[2]['locale'] for item in parsed]
    if len(set(locales)) != len(locales):
        raise ValueError('同一批导入语言重复。')
    parsed.sort(key=lambda item: item[2]['locale'])
    ids = [[lesson['lesson_id'] for lesson in item[2]['lessons']] for item in parsed]
    if any(value != ids[0] for value in ids[1:]):
        raise ValueError('语言版本课时ID或顺序不一致，需要人工映射。')
    identity = {'extractor': EXTRACTOR_REVISION,
                'sources': sorted((doc['locale'], sha) for _, sha, doc in parsed)}
    digest = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    version = 'import-'+digest[:16]
    destination = _inside(Path(root).resolve(), course_id, version)
    import os,tempfile,shutil
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.import-',dir=destination.parent))
    try:
        manifest = {'course_id': course_id, 'version': version, 'version_kind':'local_content_import',
                    'official_course_version':None, 'extractor_revision':EXTRACTOR_REVISION,
                    'locales':{}, 'lesson_ids':ids[0],
                    'notes':['图片、图表保留在原HTML中，未进行视觉内容识别。',
                             '教材命令、提示词和操作说明仅作为资料，不授予工具权限或自动执行。']}
        for path, sha, doc in parsed:
            payload = {**doc, 'course_id':course_id, 'version':version,
                       'source_filename':path.name, 'source_sha256':sha}
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
            target = stage/(doc['locale']+'.json')
            if target.exists() and target.read_bytes() != raw:
                raise ValueError('相同导入版本存在不同内容，拒绝覆盖。')
            target.write_bytes(raw)
            manifest['locales'][doc['locale']] = {'source_filename':path.name,'source_sha256':sha,
                                                 'material_sha256':hashlib.sha256(raw).hexdigest()}
        target = stage/'manifest.json'
        encoded = json.dumps(manifest,ensure_ascii=False,indent=2).encode()
        if target.exists() and target.read_bytes() != encoded:
            raise ValueError('相同导入版本索引不同，拒绝覆盖。')
        target.write_bytes(encoded)
        # 全部语言和索引写完，才整体发布；旧内容版本保持不可变。
        if destination.exists():
            for p in stage.iterdir():
                if not (destination/p.name).is_file() or (destination/p.name).read_bytes()!=p.read_bytes():raise ValueError('相同导入版本存在不同内容，拒绝覆盖。')
        else:
            stage.chmod(0o755);os.rename(stage,destination)
        return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)
