"""新运行目录登记/租约与有界清理；旧目录和未知运行不自动接管。"""
from pathlib import Path
from datetime import datetime,timezone
import os,json,time,stat,hashlib,fcntl,re
from pydantic import BaseModel,ConfigDict,Field

NAME=re.compile(r'^(?:direct-)?[0-9a-f]{32}$')
MARKER='.run-owner.json';LEASE='.run-lease'

class CleanupLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_entries:int=Field(default=64,ge=1,le=256)
    max_runs:int=Field(default=10,ge=1,le=32)
    max_files:int=Field(default=256,ge=1,le=512)
    max_bytes:int=Field(default=32*1024*1024,ge=1,le=64*1024*1024)
    max_depth:int=Field(default=8,ge=1,le=12)
    min_age_seconds:float=Field(default=86400.0,ge=0,le=31536000)
    deadline_seconds:float=Field(default=2.0,gt=0,le=10)

class RunLease:
    """父进程持有至本轮所有子进程/结果处理退出；只有可信已知终态可清理。"""
    def __init__(self,path,kind):
        self.path=Path(path).absolute();self.outcome='unknown'
        if self.path.resolve()!=self.path or not NAME.fullmatch(self.path.name):raise ValueError('unsafe_run_directory')
        self.fd=os.open(self.path/LEASE,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.record={'schema':'owned-run-v1','run_id':self.path.name,'kind':kind,'uid':os.getuid(),
          'root_dev':self.path.parent.stat().st_dev,'root_ino':self.path.parent.stat().st_ino,
          'created_at':time.time(),'finished_at':None,'outcome':'active'}
        try:self._save()
        except BaseException:os.close(self.fd);raise
    def _save(self):
        tmp=self.path/'.run-owner.pending';raw=json.dumps(self.record,separators=(',',':')).encode()
        fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(tmp,self.path/MARKER)
    def close(self,outcome):
        if outcome not in ('completed','failed_known','unknown'):raise ValueError('invalid_run_outcome')
        try:self.record.update(outcome=outcome,finished_at=time.time());self._save()
        finally:os.close(self.fd)

def _open_directory(name,*,parent=None):
    return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)

class RunInventory:
    def __init__(self,root,limits=None):
        self.root=Path(root).absolute();self.limits=CleanupLimits.model_validate(limits or {})
        if self.root.resolve()!=self.root or self.root.is_symlink():raise ValueError('unsafe_run_root')
        st=self.root.stat();self._root_identity=(st.st_dev,st.st_ino)
    def _root_fd(self):
        fd=_open_directory(self.root);st=os.fstat(fd)
        if (st.st_dev,st.st_ino)!=self._root_identity:os.close(fd);raise ValueError('run_root_changed')
        return fd
    def _tree(self,fd,deadline):
        rows=[];total=0
        def walk(current,prefix,depth):
            nonlocal total
            if depth>self.limits.max_depth:raise ValueError('inventory_limit')
            with os.scandir(current) as scan:
                for item in scan:
                    if time.monotonic()>deadline or len(rows)>=self.limits.max_files:raise ValueError('inventory_limit')
                    st=os.stat(item.name,dir_fd=current,follow_symlinks=False);name=prefix+item.name
                    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)) or stat.S_ISREG(st.st_mode) and st.st_nlink!=1:raise ValueError('unsafe_run_entry')
                    rows.append([name,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns,stat.S_ISDIR(st.st_mode),None])
                    if stat.S_ISDIR(st.st_mode):
                        child=_open_directory(item.name,parent=current)
                        try:walk(child,name+'/',depth+1)
                        finally:os.close(child)
                    else:
                        total+=st.st_size
                        if total>self.limits.max_bytes:raise ValueError('inventory_limit')
                        source=os.open(item.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=current);digest=hashlib.sha256();read=0
                        try:
                            while True:
                                if time.monotonic()>deadline:raise ValueError('inventory_limit')
                                block=os.read(source,65536)
                                if not block:break
                                read+=len(block)
                                if read>st.st_size:raise ValueError('run_changed')
                                digest.update(block)
                            after=os.fstat(source)
                            if (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns):raise ValueError('run_changed')
                            rows[-1][6]=digest.hexdigest()
                        finally:os.close(source)
        walk(fd,'',0);return sorted(rows),total
    def _inspect(self,rootfd,name,deadline):
        fd=_open_directory(name,parent=rootfd);lock=None
        try:
            st=os.fstat(fd)
            if st.st_uid!=os.getuid():raise ValueError('foreign_run_owner')
            mfd=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
            try:raw=os.read(mfd,4097)
            finally:os.close(mfd)
            if len(raw)>4096:raise ValueError('invalid_run_marker')
            row=json.loads(raw)
            if not isinstance(row,dict):raise ValueError('invalid_run_marker')
            root_stat=os.fstat(rootfd)
            if (row.get('schema'),row.get('run_id'),row.get('uid'),row.get('root_dev'),row.get('root_ino'))!=('owned-run-v1',name,os.getuid(),root_stat.st_dev,root_stat.st_ino):raise ValueError('foreign_run_owner')
            if row.get('outcome') not in ('completed','failed_known'):raise ValueError('active_or_unknown')
            if type(row.get('finished_at')) not in (int,float) or not 0<=time.time()-row['finished_at']>=self.limits.min_age_seconds:raise ValueError('too_recent_or_clock_unknown')
            lock=os.open(LEASE,os.O_RDWR|os.O_NOFOLLOW,dir_fd=fd);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            rows,total=self._tree(fd,deadline);token=hashlib.sha256(json.dumps([st.st_ino,rows],separators=(',',':')).encode()).hexdigest()
            return fd,lock,rows,{'run_id':name,'bytes':total,'files':len(rows),'token':token}
        except BaseException:
            if lock is not None:os.close(lock)
            os.close(fd);raise
    def preview(self):
        deadline=time.monotonic()+self.limits.deadline_seconds;items=[];skipped=0;seen=0;partial=False;rootfd=self._root_fd()
        try:
            with os.scandir(rootfd) as scan:
                for entry in scan:
                    if seen>=self.limits.max_entries or len(items)>=self.limits.max_runs or time.monotonic()>deadline:partial=True;break
                    seen+=1
                    if not NAME.fullmatch(entry.name):skipped+=1;continue
                    try:fd,lock,rows,item=self._inspect(rootfd,entry.name,deadline)
                    except (OSError,ValueError,KeyError,TypeError):skipped+=1;continue
                    os.close(lock);os.close(fd)
                    if sum(x['bytes'] for x in items)+item['bytes']>self.limits.max_bytes:partial=True;break
                    items.append(item)
            return {'items':items,'seen':seen,'skipped':skipped,'partial':partial,'known_eligible_bytes':sum(x['bytes'] for x in items),'total_runtime_bytes':None,'note':'仅有界可清理样本；活动/未知/旧未登记目录保留，不能据此断言总运行空间。'}
        finally:os.close(rootfd)
    def apply(self,items):
        """逐目录复核预览和租约；只通过目录FD删除，不跟随路径中的替换符号链接。"""
        if type(items) is not list or len(items)>self.limits.max_runs:raise ValueError('cleanup_limit')
        deadline=time.monotonic()+self.limits.deadline_seconds;rootfd=self._root_fd();removed=[];total=0
        try:
            for item in items:
                name=item.get('run_id')
                if not isinstance(name,str) or not NAME.fullmatch(name):raise ValueError('unsafe_run_directory')
                fd,lock,rows,current=self._inspect(rootfd,name,deadline)
                try:
                    if item!=current or total+current['bytes']>self.limits.max_bytes:raise ValueError('cleanup_preview_changed')
                    # 所有权标记与租约最后移除；中断后仍可重新预览剩余文件。
                    for row in sorted(rows,key=lambda r:(r[0].count('/'),len(r[0])),reverse=True):
                        path,ino,size,mtime,ctime,isdir,content_hash=row
                        if path in (MARKER,LEASE):continue
                        if time.monotonic()>deadline:raise ValueError('cleanup_deadline')
                        parts=path.split('/');parent=os.dup(fd)
                        try:
                            for component in parts[:-1]:
                                child=_open_directory(component,parent=parent);os.close(parent);parent=child
                            st=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
                            if st.st_ino!=ino or stat.S_ISLNK(st.st_mode) or not isdir and (st.st_size,st.st_mtime_ns,st.st_ctime_ns)!=(size,mtime,ctime):raise ValueError('cleanup_preview_changed')
                            if isdir:os.rmdir(parts[-1],dir_fd=parent)
                            else:os.unlink(parts[-1],dir_fd=parent)
                        finally:os.close(parent)
                    os.unlink(MARKER,dir_fd=fd);os.unlink(LEASE,dir_fd=fd)
                    if os.stat(name,dir_fd=rootfd,follow_symlinks=False).st_ino!=os.fstat(fd).st_ino:raise ValueError('cleanup_preview_changed')
                    os.rmdir(name,dir_fd=rootfd);removed.append(name);total+=current['bytes']
                finally:os.close(lock);os.close(fd)
            return {'removed':removed,'bytes':total,'preview_verified':True}
        finally:os.close(rootfd)
