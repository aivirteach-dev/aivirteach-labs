# DSH 受限账号验证 · 2026-10-29

`dsh-runner` 为专用无交互登录账号，用户组仅dsh-runner。保留xf作为开发和管理账号，未修改用户组、sudo、Docker或防火墙。

为读取现有项目使用ACL：/home/xf仅遍历；/home/xf/projects/dsh允许列出顶层；dsh_tutor源码和模型配置可读不可写；sandbox/results/runner-check是专用测试可写目录。原ACL备份见development/runner-acl-before-20261029.txt。未来改权限前应先检查现行ACL，不能盲目恢复旧记录而覆盖后续修改。

验证命令：
```bash
sudo -u dsh-runner /home/xf/projects/dsh/.venv/bin/python /home/xf/projects/dsh/tools/runner_verify.py
```
首次因为项目根目录不可读取导致Python找不到模块；修正根目录读取权限后复测通过。8项os.access/身份检查为真，DSH→Ollama返回连接成功，耗时24.34秒；随后ps未列出dsh-runner进程。完整结果见development/runner-validation-20261029.json。

这些检查不能证明所有文件和网络路径均受限；现有常规启动命令未强制使用dsh-runner，仅本次验证通过sudo指定该账号。未验证MCP、VM共享、完整网络、远端模型取消或真实课程VM。

## 受限启动入口（2026-10-29）

config.json 的 execution_user 当前为 dsh-runner。正常调用 run_prompt 和直接启动工作进程时都会检查当前账号；xf 的实际调用在创建运行目录之前被拒绝。此检查防止误操作，不能替代 Linux 文件权限，也不能当作攻击隔离。

在 Ubuntu 中运行简单对话：
```bash
cd /home/xf/projects/dsh
sudo -u dsh-runner .venv/bin/python -m sandbox.hello
```
如需密码，输入 xf 的 Ubuntu 密码。以 dsh-runner 运行的模型调用与8项自动测试已通过。模拟课程文件仅授予只读权限，输出位于 sandbox/results/runner-check。尚未验证完整课程演示入口，后续接入前须实际运行。

当前不改动系统防火墙：UFW查询结果为inactive，Docker和iptables/nft规则尚需进一步核对；VMware共享/剪贴板/拖放状态尚待确认。
