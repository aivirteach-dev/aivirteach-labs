"""教材接口回归：精确版本/语言、来源保真、长代码摘录与边界拒绝。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dsh_tutor.materials import extract_html, import_bilingual, MaterialRepository
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.course import build_prompt


def page(locale, *, lesson='lesson-setup', extra='', code='port: 5678'):
    return (f'<html lang="{locale}"><script>throw new Error("must not execute")</script>'
            f'<nav>0 / 9</nav><template id="{lesson}"><h1>Setup</h1>'
            '<p>Open <strong>n8n</strong> in your browser.</p>'
            f'<pre><code>{code}</code></pre>{extra}</template></html>').encode()


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.en=self.base/'en.html';self.en.write_bytes(page('en'))
        self.zh=self.base/'zh.html';self.zh.write_bytes(page('zh-CN'))
        self.root=self.base/'materials'
        self.manifest=import_bilingual([self.en,self.zh],self.root,'course')
        self.version=self.manifest['version']
        self.repo=MaterialRepository(self.root)

    def test_extracts_templates_ignores_ui_and_scripts_preserves_code(self):
        doc=extract_html(page('en',code='a &lt; b\n  exact indentation'))
        text='\n'.join(x['text'] for x in doc['lessons'][0]['blocks'])
        self.assertNotIn('must not execute',text)
        self.assertNotIn('0 / 9',text)
        self.assertIn('Open n8n in your browser.',text)
        self.assertEqual(doc['lessons'][0]['blocks'][1]['text'],'a < b\n  exact indentation')

    def test_import_is_idempotent_and_input_order_independent(self):
        second=import_bilingual([self.zh,self.en],self.root,'course')
        self.assertEqual(second,self.manifest)

    def test_copy_address_button_is_content_but_copy_ui_is_not(self):
        extra='<p>Visit <button class="inline-copy-link" data-copy-text="http://localhost:5678">http://localhost:5678</button>.</p><button class="copy-btn">Copy</button>'
        text='\n'.join(x['text'] for x in extract_html(page('en',extra=extra))['lessons'][0]['blocks'])
        self.assertIn('Visit http://localhost:5678.',text)
        self.assertNotIn('Copy',text)

    def test_no_implicit_version_language_or_lesson_fallback(self):
        for version,locale,lesson in [('v5','en','lesson-setup'),
                                      (self.version,'en-US','lesson-setup'),
                                      (self.version,'en','same-title-but-wrong-id')]:
            with self.subTest(version=version,locale=locale,lesson=lesson):
                with self.assertRaises((ValueError,FileNotFoundError)):
                    self.repo.prepare('course',version,locale,lesson,'port?')

    def test_tampered_material_rejected(self):
        p=self.root/'course'/self.version/'en.json'
        p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'指纹'):
            self.repo.load('course',self.version,'en')

    def test_path_traversal_and_outside_symlink_rejected(self):
        for course in ['../outside','a/b','..']:
            with self.assertRaises(ValueError):self.repo.load(course,self.version,'en')
        (self.root/'outside').symlink_to(self.base,target_is_directory=True)
        with self.assertRaises(ValueError):self.repo.load('outside',self.version,'en')

    def test_mismatched_language_units_rejected(self):
        self.zh.write_bytes(page('zh-CN',lesson='lesson-other'))
        with self.assertRaisesRegex(ValueError,'课时ID'):
            import_bilingual([self.en,self.zh],self.root,'course')

    def test_duplicate_locale_or_template_rejected(self):
        with self.assertRaises(ValueError):import_bilingual([self.en,self.en],self.root,'course')
        raw=page('en')
        raw=raw.replace(b'</html>',b'<template id="lesson-setup"><h1>Duplicate</h1></template></html>')
        with self.assertRaises(ValueError):extract_html(raw)

    def test_provenance_and_locale_enter_prompt(self):
        snapshot=self.repo.prepare('course',self.version,'en','lesson-setup','port?')
        self.assertEqual(snapshot.material.source_sha256,hashlib.sha256(self.en.read_bytes()).hexdigest())
        self.assertEqual(snapshot.material.anchor,'lesson-setup')
        self.assertEqual(snapshot.goals,[])  # 不从标题编造学习目标。
        prompt=build_prompt(CourseQuestion(request_id='r',course=snapshot,question='port?'))
        self.assertIn('"locale": "en"',prompt)
        self.assertNotIn('请用简短中文回答',prompt)
        self.assertEqual(len(snapshot.material.excerpts),len(snapshot.instructions))

    def test_long_code_is_stored_complete_but_prompt_is_bounded(self):
        code='\n'.join(f'const value{i} = {i};' for i in range(1500))+'\nconst uniqueTarget = 12345;'
        self.en.write_bytes(page('en',code=code))
        self.zh.write_bytes(page('zh-CN',code=code))
        manifest=import_bilingual([self.en,self.zh],self.root,'course')
        full=self.repo.load('course',manifest['version'],'en')
        self.assertEqual(full['lessons'][0]['blocks'][1]['text'],code)
        snapshot=self.repo.prepare('course',manifest['version'],'en','lesson-setup','uniqueTarget?')
        self.assertNotIn('uniqueTarget','\n'.join(snapshot.instructions))
        self.assertEqual(snapshot.material.omitted_code_blocks,1)
        self.assertTrue(snapshot.material.excerpt_only)
        self.assertLessEqual(sum(map(len,snapshot.instructions)),1400)
        full_blocks={x['id']:x['text'] for x in full['lessons'][0]['blocks']}
        for text,ref in zip(snapshot.instructions,snapshot.material.excerpts):
            self.assertEqual(text,full_blocks[ref.block_id][ref.start:ref.end])
        self.assertLess(len(build_prompt(CourseQuestion(request_id='r',course=snapshot,question='uniqueTarget?'))),10000)

    def test_no_cross_lesson_retrieval(self):
        self.en.write_bytes(page('en').replace(b'</html>',b'<template id="lesson-private"><h1>Other</h1><p>secret-other-lesson</p></template></html>'))
        self.zh.write_bytes(page('zh-CN').replace(b'</html>',b'<template id="lesson-private"><h1>Other</h1><p>secret-other-lesson</p></template></html>'))
        manifest=import_bilingual([self.en,self.zh],self.root,'course')
        snapshot=self.repo.prepare('course',manifest['version'],'en','lesson-setup','secret-other-lesson')
        self.assertNotIn('secret-other-lesson','\n'.join(snapshot.instructions))

    def test_label_and_code_value_are_retrieved_together(self):
        extra='<h2>Check your timezone</h2><p>The course currently uses:</p><pre>Asia/Kuala_Lumpur</pre><p>Use the same timezone in the Code node.</p>'
        self.en.write_bytes(page('en',extra=extra))
        self.zh.write_bytes(page('zh-CN',extra=extra))
        manifest=import_bilingual([self.en,self.zh],self.root,'course')
        snapshot=self.repo.prepare('course',manifest['version'],'en','lesson-setup','Which timezone does the course use?')
        self.assertIn('Asia/Kuala_Lumpur','\n'.join(snapshot.instructions))
