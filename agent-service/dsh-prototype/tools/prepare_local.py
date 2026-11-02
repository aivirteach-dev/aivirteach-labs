"""候选包本地准备：只建立忽略配置和固定 Node 链接，不启动模型或安装软件。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = 'v22.13.0'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-path', default=shutil.which('node'))
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('当前运行权限及启动器仅验收 Linux；不要在 Windows 上当成隔离环境运行。')
    if not args.node_path:
        parser.error('请先安装 Node 22.13.0，再通过 --node-path 指定可执行文件。')
    node = Path(args.node_path).expanduser().resolve(strict=True)
    version = subprocess.run([str(node), '--version'], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    if version != NODE_VERSION:
        parser.error('Node 必须为已验证版本 ' + NODE_VERSION)
    target = ROOT / '.runtime/node-v22.13.0-linux-x64/bin/node'
    if target.exists() or target.is_symlink():
        if target.resolve() != node:
            parser.error('目标 Node 路径已存在且不同，停止以免覆盖。')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(node)
    # Git 不保存空目录，旧维护回归需要此忽略目录作为临时样例的父目录。
    (ROOT / 'sandbox/results').mkdir(mode=0o700, parents=True, exist_ok=True)
    config = ROOT / 'config.json'
    if config.exists():
        print('保留既有 config.json，不覆盖。')
    else:
        sample = json.loads((ROOT / 'config/runtime.example.json').read_text(encoding='utf-8'))
        # 独占创建并收紧权限；正式身份不因本机开发而降为当前账号。
        with open(config, 'x', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as out:
            json.dump(sample, out, ensure_ascii=False, indent=2)
            out.write('\n')
        print('已生成本机测试配置：仅指向 127.0.0.1:1，正式身份仍为 dsh-runner。')
    print('Node 路径已就绪；未安装依赖、连接模型或启动服务。')


if __name__ == '__main__':
    main()
