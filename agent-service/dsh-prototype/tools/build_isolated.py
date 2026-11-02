"""显式串行构建隔离镜像：CPU1/内存2GiB，不启动服务、不更改全局Docker配置。

本入口针对当前仍支持旧构建器限额的服务器；使用者须显式选择。
Dockerfile也能由受限BuildKit/CI构建，未来更换构建器不改变Agent架构。
"""
from pathlib import Path
import argparse
import json
import os
import re
import shutil
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-limited',action='store_true',help='明确同意本次命令使用已弃用但当前可用的受限旧构建器')
    parser.add_argument('--network',choices=['default','host'],default='default',help='仅构建步骤；本机代理场景可显式选host，不改变运行网络')
    parser.add_argument('--tag',default='stage2')
    args=parser.parse_args()
    if not args.legacy_limited:parser.error('请使用受限CI/BuildKit，或显式选择--legacy-limited；不做无限额回退')
    if not re.fullmatch('[a-z0-9][a-z0-9_.-]{0,63}',args.tag):parser.error('invalid_image_tag')
    if os.name!='posix' or not Path('/proc/meminfo').exists():parser.error('本入口仅针对已核对的Linux主机')
    parent=ROOT/'.isolated'
    if parent.is_symlink() or not (parent/'config/host.json').is_file():parser.error('先在本机显式准备.isolated；不自动创建或覆盖私有目录')
    if shutil.disk_usage(ROOT).free<4*1024**3:parser.error('磁盘余量不足，停止构建，不自动清理')
    free=[line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')]
    if not free or int(free[0])*1024<4*1024**3:parser.error('可用内存不足，停止构建')
    if os.getloadavg()[0]>max(1,(os.cpu_count() or 1)*.75):parser.error('主机已有较高负载，延后构建')
    logs=parent/('build-'+str(time.time_ns()));logs.mkdir(mode=0o700)
    env=dict(os.environ,DOCKER_BUILDKIT='0')
    rows=[]
    for target,image in [('agent','dsh-test-agent'),('frontend','dsh-test-management'),('access','dsh-test-access')]:
        command=['docker','build','--memory','2g','--memory-swap','2g','--cpu-period','100000','--cpu-quota','100000',
                 '--cpu-shares','128','--force-rm','--network',args.network,'-f','deploy/Dockerfile','--target',target,'-t',image+':'+args.tag]
        # 仅传变量名称，值由当前进程环境提供；不打印带凭据的代理URL。
        for name in ['HTTP_PROXY','HTTPS_PROXY','NO_PROXY']:
            if name in env:command+=['--build-arg',name]
        print('building_isolated_target '+target,flush=True)
        started=time.monotonic()
        with (logs/(target+'.log')).open('xb') as output:
            try:result=subprocess.run(command+['.'],cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=900);code=result.returncode
            except subprocess.TimeoutExpired:code=124
        rows.append({'target':target,'exit_code':code,'seconds':round(time.monotonic()-started,2)})
        (logs/'status.json').write_text(json.dumps(rows,indent=2)+'\n')
        if code:raise SystemExit('isolated_build_failed; inspect_private_build_logs')
    print('isolated_images_built; no_services_started')


if __name__=='__main__':main()
