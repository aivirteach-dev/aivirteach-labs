# Ubuntu 开发环境迁移记录

主要开发位置：`/home/xf/projects/dsh`；账号 xf；主机 xf-virtual-machine。Windows `D:\MAIC\DSH` 保留迁移前原件，后续不双边开发。

迁移了692个源码、文档、模拟资料、历史样例和参考代码文件；包与逐文件SHA256全部核对。未迁入Windows .venv、缓存、旧运行目录、Git内部数据或凭据。参考仓库是固定提交快照，提交号在 development/migration-source-manifest.json。

Linux Python 3.10.12，依赖按原requirements固定版本重建。Ubuntu缺少ensurepip，使用PyPA官方get-pip.py仅安装到项目.venv；没有sudo修改系统Python。新增POSIX独立进程组清理，并测试组长退出后结束子进程且不影响另一个请求。Windows路径仍保留，迁移后Windows分支未重新运行。

验证结果：6项基础检查通过；Ubuntu直接请求Ollama得到“连接成功”（6.17秒）；Ubuntu Python→DSH→Ollama得到“连接成功”（7.95秒，0工具调用），调用前后未发现该环境的DSH运行进程残留。Ollama 0.35.1，qwen3.5:9b。直接API探测指定空闲5分钟卸载；DSH链路仍使用服务端默认驻留策略，没有修改Mac配置。

详细证据：development/migration-20261029.json、latest-tests.json、ubuntu-ollama-probe.json、ubuntu-dsh-probe.json。

边界：未做MCP工具桥接、多人、Windows Gateway或AIVirTeach端到端验证。未完整审查虚拟机剪贴板/拖放及网络隔离；未观察到hgfs共享挂载，不代表所有共享功能已关闭。远端推理取消仍未验证。Linux开发环境不等于Linux课程专用，课程VM仍需分别适配Windows和Linux。

运行：
```bash
cd /home/xf/projects/dsh
.venv/bin/python tools/dashboard.py test
.venv/bin/python -m sandbox.hello
.venv/bin/python tools/dashboard.py build
```
