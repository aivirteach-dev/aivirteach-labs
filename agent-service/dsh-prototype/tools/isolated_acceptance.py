"""只验收本目录dsh-test环境：虚构身份、固定假模型、一次受控Agent重启。

不创建/迁移/删除数据，不操作其他Compose项目，不连接真实模型或VM。
首次启动且无其他测试任务时运行；输出只含结果，不输出身份令牌或Cookie。
"""
from pathlib import Path
import http.cookiejar
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
COMPOSE=['docker','compose','-p','dsh-test','-f','compose.isolated.yaml']
if (ROOT/'.isolated/deployment.env').is_file():COMPOSE+=['--env-file','.isolated/deployment.env']
BASE='http://127.0.0.1:18792'


def command(args):
    result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=40)
    if result.returncode:raise RuntimeError('isolated_command_failed')
    return result.stdout.strip()


def request(path,*,token=None,body=None):
    headers={}
    if token:headers['Authorization']='Bearer '+token
    data=None
    if body is not None:data=json.dumps(body).encode();headers['Content-Type']='application/json'
    req=urllib.request.Request(BASE+path,data=data,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def body_json(response,*,error=False):
        raw=response.read(65537)
        if len(raw)>65536:raise RuntimeError('isolated_response_too_large')
        try:return json.loads(raw)
        except (ValueError,UnicodeError):
            # 重启短窗口中入口可返回502 HTML；只保留类别，不打印正文或当作成功。
            if error:return {'error':'non_json_upstream_error'}
            raise RuntimeError('isolated_invalid_json') from None
    try:
        with opener.open(req,timeout=5) as r:return r.status,body_json(r)
    except urllib.error.HTTPError as e:return e.code,body_json(e,error=True)


def wait_alive():
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        try:
            if request('/health')[0]==200:return
        except OSError:pass
        time.sleep(.5)
    raise RuntimeError('isolated_startup_timeout')


def stats():
    text=command(COMPOSE+['exec','-T','fake-provider','python','-c',
        "import json,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:18080/stats',timeout=2).read().decode())"])
    return json.loads(text)


def run():
    checks=[]
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name)
    ids=json.loads(command(COMPOSE+['config','--format','json']))
    check('explicit_test_project',ids['name']=='dsh-test' and ids['networks']['isolated']['internal'])
    limits={'agent':(2147483648,1000000000,192),'fake-provider':(134217728,250000000,32),
            'management':(536870912,500000000,64),'access':(134217728,250000000,32)}
    for service in ['agent','fake-provider','management','access']:
        cid=command(COMPOSE+['ps','-q',service]);check('only_owned_container_'+service,bool(cid))
        labels=json.loads(command(['docker','inspect','--format','{{json .Config.Labels}}',cid]))
        check('owned_directory_'+service,labels.get('com.docker.compose.project')=='dsh-test' and labels.get('com.docker.compose.project.working_dir')==str(ROOT))
        # 不读取整个inspect或容器环境，避免将能力令牌带进报告。
        values=command(['docker','inspect','--format','{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}',cid]).split('|')
        check('effective_limits_'+service,values[0]=='1000:1000' and values[1]=='true' and tuple(map(int,values[2:]))==limits[service])
        security=command(['docker','inspect','--format','{{.HostConfig.Privileged}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}',cid]).split('|')
        check('effective_security_'+service,security[0]=='false' and json.loads(security[1])==['ALL'] and any(x in ('no-new-privileges','no-new-privileges:true') for x in json.loads(security[2])))
        networks=json.loads(command(['docker','inspect','--format','{{json .NetworkSettings.Networks}}',cid]))
        expected_networks={'dsh-test_isolated','dsh-test_access'} if service=='access' else {'dsh-test_isolated'}
        check('effective_network_'+service,set(networks)==expected_networks)
        mounts=json.loads(command(['docker','inspect','--format','{{json .Mounts}}',cid]))
        check('mounts_only_private_test_data_'+service,all(Path(x['Source']).resolve().is_relative_to(ROOT/'.isolated') and (not x['RW'] or x['Destination']=='/state') for x in mounts if x['Type']=='bind'))
        ports=json.loads(command(['docker','inspect','--format','{{json .HostConfig.PortBindings}}',cid])) or {}
        expected_ports={'13002','18792'} if service=='access' else set()
        check('published_ports_only_loopback_'+service,{x['HostPort'] for v in ports.values() for x in v}==expected_ports and all(x['HostIp']=='127.0.0.1' for v in ports.values() for x in v))
    tokens=[(ROOT/'.isolated/secrets'/name).read_text().strip() for name in ['test-a.token','test-b.token']]
    wait_alive();initial=stats()['requests']
    check('starts_paused',request('/ready')[0]==503)
    for _ in range(3):request('/health');request('/ready')
    check('probes_do_not_infer',stats()['requests']==initial)
    check('student_api_requires_token',request('/v1/tutor/capabilities')[0]==401)
    check('wrong_token_rejected',request('/v1/tutor/capabilities',token='x'*43)[0]==401)
    check('runs_as_restricted_identity',command(COMPOSE+['exec','-T','agent','id','-un'])=='dsh-runner')
    command(COMPOSE+['exec','-T','agent','python','-m','dsh_tutor.isolated_host','verify'])
    check('explicit_verify_enables_readiness',request('/ready')[0]==200)
    course=json.loads(command(COMPOSE+['exec','-T','agent','python','-c','import json;from dsh_tutor.isolated_host import COURSE;print(json.dumps(COURSE.model_dump()))']))
    body={'api_version':'dsh-tutor.v1','payload':{'request_id':'environment-'+uuid4().hex,'question':'请返回隔离环境固定测试回答。','course':course}}
    code,op=request('/v1/tutor/operations',token=tokens[0],body=body)
    check('submit_accepted',code==202);operation=op['operation_id']
    end=time.monotonic()+70
    while time.monotonic()<end:
        time.sleep(1);code,op=request('/v1/tutor/operations/'+operation,token=tokens[0])
        if op.get('state') in ('succeeded','failed','recovery_required'):break
    check('actual_dsh_and_two_saves',op.get('state')=='succeeded' and op['result']['persistence']=={'agent_chat':'saved','operation_record':'saved','platform':'unconfirmed'})
    check('fixed_fake_not_real_model',op['result']['answer']=='这是隔离环境的固定测试回答，不是真实模型推理。' and stats()['requests']==initial+1)
    check('usage_not_faked_as_zero',op['result']['usage']['state']=='unknown')
    code,replay=request('/v1/tutor/operations',token=tokens[0],body=body)
    check('resubmit_same_operation',code==200 and replay['operation_id']==operation and stats()['requests']==initial+1)
    code,_=request('/v1/tutor/operations/'+operation,token=tokens[1]);check('other_student_cannot_read',code in (403,404))
    conflict=json.loads(json.dumps(body));conflict['payload']['question']='更改内容但重复编号'
    check('changed_body_same_id_rejected',request('/v1/tutor/operations',token=tokens[0],body=conflict)[0]==409)
    cookie=http.cookiejar.CookieJar();browser=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(cookie))
    url='http://127.0.0.1:13002'
    with browser.open(urllib.request.Request(url+'/api/session',data=b'{}',headers={'Origin':url,'Content-Type':'application/json'}),timeout=5) as r:check('local_management_session',r.status==200)
    with browser.open(url+'/api/manage/overview',timeout=5) as r:
        view=json.loads(r.read(65537));check('management_reads_this_agent',r.status==200 and view['storage']['state']=='available')
    check('management_does_not_leak_tokens',all(t not in json.dumps(view) for t in tokens))
    command(COMPOSE+['restart','agent']);wait_alive()
    check('restart_is_paused',request('/ready')[0]==503)
    code,restored=request('/v1/tutor/operations/'+operation,token=tokens[0])
    check('persisted_result_survives_restart',code==200 and restored['result']['answer']==op['result']['answer'])
    check('restart_and_poll_do_not_infer',stats()['requests']==initial+1)
    command(COMPOSE+['exec','-T','agent','python','-m','dsh_tutor.isolated_host','verify'])
    check('ready_after_explicit_reverification',request('/ready')[0]==200)
    report={'status':'passed','checks':checks,'fixed_fake_requests':1,'actual_model_calls':0,'tools':'disabled',
            'boundary':'environment smoke only; no real platform, VM, model quality or capacity acceptance'}
    output=ROOT/'.isolated/environment-report.json';output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':'passed','checks':len(checks),'report':str(output),'actual_model_calls':0}))


if __name__=='__main__':run()
