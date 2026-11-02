"""显式准备新隔离测试目录；只生成虚构身份与随机本地能力令牌，不启动服务。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import secrets


def prepare(path):
    path=Path(path).absolute()
    if path.exists() or path.is_symlink():raise ValueError('directory_already_exists')
    path.mkdir(mode=0o700)
    for child in ['state','config','secrets']:(path/child).mkdir(mode=0o700)
    rows=[]
    for name in ['test-a','test-b']:
        token=secrets.token_urlsafe(32)
        fd=os.open(path/'secrets'/(name+'.token'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:out.write(token+'\n')
        rows.append({'id':name,'token_sha256':hashlib.sha256(token.encode()).hexdigest()})
    fd=os.open(path/'config/host.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as out:json.dump({'version':1,'students':rows},out)
    fd=os.open(path/'secrets/management_token',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as out:out.write(secrets.token_urlsafe(48)+'\n')


if __name__=='__main__':
    if os.name!='posix' or os.geteuid()!=1000 or os.getegid()!=1000:
        raise SystemExit('isolated_setup_requires_uid_gid_1000; adapt_mapping_without_relaxing_permissions')
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,default=Path('.isolated'))
    args=parser.parse_args()
    try:prepare(args.directory)
    except (OSError,ValueError):raise SystemExit('isolated_setup_refused; do not overwrite existing data')
    print('isolated_configuration_created; tokens_not_printed; initialize_explicitly_next')
