"""检查当前安装版本合成后的真实DSH插件树；不请求模型、不读默认用户home。"""
from pathlib import Path
import importlib.metadata as meta
import json,os,re,subprocess,tempfile,sys,hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dsh_tutor.runtime import load_config,render_patch,build_budget_plugin,build_control_plugin


def check_profile():
    from deepseek_harness_runtime import resolve_bundled_launch_args
    with tempfile.TemporaryDirectory(prefix='tutor-profile-') as directory:
        base=Path(directory);config=load_config(ROOT/'config.json');patch=base/'runtime.patch.yml'
        patch.write_text(render_patch(config,budget_plugin=build_budget_plugin(config,base),control_plugin=build_control_plugin(base,'request-'+'a'*32)))
        result=subprocess.run([*resolve_bundled_launch_args(),'--profile','sdk-minimal','--patch',str(patch),'--dump-config'],
            cwd=base,env=dict(os.environ,DSH_HOME=str(base/'home')),capture_output=True,text=True,timeout=20)
        if result.returncode or len(result.stdout.encode())>131072:raise RuntimeError('profile_dump_failed')
        rows={}
        for row in re.split(r'(?m)(?=^- id: )',result.stdout):
            match=re.match(r'- id: ([A-Za-z0-9_-]+)\n',row)
            if match:
                if match[1] in rows:raise RuntimeError('duplicate_profile_id')
                rows[match[1]]=row
        required_disabled=('tool-fs','tool-working-directory','persistent-bash','persistent-pwsh','mcp-resources',
                           'session-log-deepseek','compaction-basic','image-offload','llm-retry')
        for name in required_disabled:
            if not re.search(r'(?m)^  disabled: true$',rows.get(name,'')):raise RuntimeError('unexpected_profile_capability:'+name)
        if 'tutor-model-budget' not in rows or re.search(r'(?m)^  disabled: true$',rows['tutor-model-budget']):
            raise RuntimeError('budget_not_mounted')
        if 'tutor-execution-control' not in rows or re.search(r'(?m)^  disabled: true$',rows['tutor-execution-control']):raise RuntimeError('control_not_mounted')
        return {'control_plugin':'tutor-execution-control','status':'passed','sdk':meta.version('deepseek-harness-sdk'),
            'runtime':meta.version('deepseek-harness-runtime-bin'),'profile':'sdk-minimal',
            'disabled':list(required_disabled),'budget_plugin':'tutor-model-budget',
            'model_calls':0,'patch_sha256':hashlib.sha256((ROOT/'ollama.patch.yml').read_bytes()).hexdigest()}


if __name__=='__main__':print(json.dumps(check_profile(),ensure_ascii=False,indent=2))
