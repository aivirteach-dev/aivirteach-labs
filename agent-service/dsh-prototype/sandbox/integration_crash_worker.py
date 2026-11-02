"""仅由验收创建的子进程：在模型中途/聊天已存而交付未存时等待被终止。

父测试只能终止自己Popen取得的PID，不扫描或终止其他Python/DSH进程。
"""
from pathlib import Path
from threading import Event
from unittest.mock import patch
import json,sys,time
from sandbox.integration_fixture import Fixture,fake_model


def main():
    root=Path(sys.argv[1]);mode=sys.argv[2];fixture=Fixture(root)
    hold=Event()
    def model(*a,**kw):
        (root/'model-count.txt').write_text('1')
        if mode=='pending':hold.wait(120)
        return fake_model(*a,**kw)
    finish=fixture.journal.finish
    def delivery(*a,**kw):
        if mode=='saved' and kw.get('state')=='succeeded':
            (root/'delivery-entered').write_text('saved');hold.wait(120)
        return finish(*a,**kw)
    with patch('dsh_tutor.runtime.run_prompt',side_effect=model),patch.object(fixture.journal,'finish',side_effect=delivery):
        fixture.service.verify_ready(upstream_idle=True)
        view,_=fixture.service.submit(fixture.bindings['A'],fixture.request())
        (root/'operation.json').write_text(json.dumps({'operation_id':view['operation_id']}))
        hold.wait(120)
    fixture.close()


if __name__=='__main__':main()
