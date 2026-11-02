"""私有教材专项；缺少对应资料时明确失败，不计入公开基础回归。"""
import json
import unittest
from pathlib import Path
from dsh_tutor.materials import MaterialRepository
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.course import build_prompt

class ActualMaterialTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).resolve().parents[2]
        self.report=json.loads((self.root/'development/materials-import-20261029.json').read_text())
        self.repo=MaterialRepository(self.root/'materials/courses')

    def test_real_pair_has_same_15_units_and_35_code_blocks(self):
        docs=[self.repo.load('ai-daily-briefing',self.report['version'],lang) for lang in ('en','zh-CN')]
        self.assertEqual([x['lesson_id'] for x in docs[0]['lessons']], [x['lesson_id'] for x in docs[1]['lessons']])
        for doc in docs:
            self.assertEqual(len(doc['lessons']),15)
            self.assertEqual(sum(b['kind']=='code' for lesson in doc['lessons'] for b in lesson['blocks']),35)
            for lesson in doc['lessons']:
                snap=self.repo.prepare('ai-daily-briefing',self.report['version'],doc['locale'],lesson['lesson_id'],'课程说明 timezone port')
                self.assertLessEqual(len(build_prompt(CourseQuestion(request_id='r',course=snap,question='课程说明 timezone port'))),10000)

    def test_real_install_port_and_delivery_timezone_retrievable(self):
        cases=[('zh-CN','lesson-1-2-install-and-start-n8n','根据本课教材，应该在Ubuntu浏览器里打开哪个地址访问n8n？只回答地址，不执行命令。','http://localhost:5678'),
               ('en','lesson-3-2-customize-delivery-and-test','Which timezone does the course use?','Asia/Kuala_Lumpur')]
        for locale,lesson,question,expected in cases:
            with self.subTest(locale=locale):
                snap=self.repo.prepare('ai-daily-briefing',self.report['version'],locale,lesson,question)
                self.assertIn(expected,'\n'.join(snap.instructions))
