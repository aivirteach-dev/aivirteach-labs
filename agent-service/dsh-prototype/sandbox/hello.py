"""简单连接测试；共用正式运行接口。"""
from pathlib import Path
from dsh_tutor.runtime import run_prompt

ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__":
    result = run_prompt("你好，请用一句简短的中文介绍你自己。", config_path=ROOT / "config.json", state_dir=ROOT / "sandbox/results/runner-check/runtime")
    print(result)
