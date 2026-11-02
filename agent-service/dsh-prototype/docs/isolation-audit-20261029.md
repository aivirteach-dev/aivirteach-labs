# 2026-10-29 隔离只读检查

基础虚拟机边界存在，但不满足准备启用 Agent 工具时约定的最小权限要求；先加固再接 MCP。

本次没有修改VMware、用户组、防火墙、Docker或SSH设置；没有读取凭据、剪贴板或业务容器数据。

- **Windows 共享目录：未发现已挂载的共享文件系统**。未发现 hgfs/9p/drvfs/cifs/nfs 挂载；这不等于证明所有共享功能都关闭。
- **剪贴板与拖放：未确认关闭**。VMX 没有明确的 copy/paste/dnd 禁用项，界面状态未可靠读出；不读取或测试个人剪贴板内容。
- **Agent 运行账号：需调整**。xf 为 UID1000，属于 sudo/docker/lxd；sudo 此次要求密码，但 Docker API 可直接访问，未报告 rootless。
- **网络：NAT；没有仅限必要目标**。可达 Mac Ollama 和 PyPI；已测宿主机22/445/3389端口超时，但不能据此证明其他主机或端口都不可达。
- **NAT 入站转发：配置中未发现条目**。vmnetnat.conf 的 incomingtcp/incomingudp 没有有效映射；没有审计所有 Windows 防火墙规则。
- **Ubuntu 防火墙：待管理员核对**。sudo -n ufw status 需要密码，不能判定防火墙已启用或关闭。没有尝试获取密码。
- **SSH 与文件权限：本轮检查符合基础要求**。项目目录和 .ssh 为700；本次连接未转发 SSH agent。SSH监听所有来宾网卡；实际可达范围仍受网络与防火墙影响。
- **DSH 权限边界：工具关闭；未建立独立低权限运行边界**。默认文件和Shell工具在补丁中关闭；项目仍由xf运行，没有专用受限账号或项目目录强制隔离。

## 下一步建议

1. 保留xf作为开发/管理账号，为DSH及工具建立无sudo/docker/lxd权限的专用运行账号或等效受限服务。不要随意移除xf现有组或停用已有Docker服务。
2. 显式关闭VMware共享文件夹、双向剪贴板和拖放；需要用户输入的管理员密码由用户自行输入。
3. 区分开发安装与运行期网络；运行期只放行必要的SSH管理和Ollama访问。先设计规则，避免切断当前连接或影响其他服务。
4. 核对防火墙及恢复点，再复查实际限制，之后开始一个MCP模拟工具。

Docker高权限指Ubuntu内部权限，不等于已获得Windows宿主机权限。详见 development/isolation-audit-20261029.json；Docker官方说明：https://docs.docker.com/engine/install/linux-postinstall/
