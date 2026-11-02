# 第二阶段：独立容器环境

本阶段只验证隔离环境装配。默认只有两个虚构身份、一份虚构课程、一个固定假提供方，没有真实学生账号、真实模型、Gateway 或 VM 权限。端到端平台认证、真实模型质量和容量留待后续阶段。

```text
本机测试客户端 + 专用测试令牌
             │ SSH转发 / 服务器loopback:18792
             ▼
      access入口容器 ───── agent容器 ───── fake-provider容器
      只转发固定目标       同一个DSH/调度/记忆       固定回答，不做推理
             │               │
             │               └─ .isolated/state 私有虚构库与运行目录
             │
浏览器 ─ loopback:13002 ─ management容器
                        Next管理界面 ─ agent:18788 只读管理接口
```

Agent、假提供方和管理前端只接内部网络。只有入口容器另接发布网络，且主机端口明确绑定127.0.0.1。Nginx配置只转发两个固定目标，没有通用URL代理或宿主配置挂载。此布局兼容当前Docker对纯internal网络不生成可访问主机端口映射的行为，不通过开放Agent出站网络解决。

## 资源和权限

| 容器 | CPU上限 | 内存/交换总上限 | PID上限 |
| --- | --- | --- | --- |
| agent | 1 | 2 GiB / 2 GiB（不额外使用交换） | 192 |
| fake-provider | 0.25 | 128 MiB / 128 MiB | 32 |
| management | 0.5 | 512 MiB / 512 MiB | 64 |
| access | 0.25 | 128 MiB / 128 MiB | 32 |

总CPU上限2核、运行内存上限合计2.75GiB；这是上限，不是预留量或容量承诺。模型并发1、最多4等待、每学生1。均为UID/GID 1000、只读根文件系统、cap_drop ALL、no-new-privileges、有限tmpfs和日志；不挂Docker/libvirt socket或原项目目录。agent内用户名为dsh-runner。宿主准备者也必须为UID/GID1000，否则先另行适配映射，不放宽文件权限。

构建基础镜像版本与digest固定在Dockerfile；Python/Node和应用依赖保持回归基线。Debian系统包安装仍依赖当时软件源，不能宣称跨时间逐字节重建相同镜像；记录最终镜像ID。构建时按CPU1/内存2GiB串行执行，避免同时运行多个大型构建。

官方行为依据：[Compose资源限制](https://docs.docker.com/reference/compose-file/services/)、[Buildx构建器限额选项](https://docs.docker.com/build/builders/drivers/docker-container/)。当前主机Buildx不提供--resource；本轮仅命令级DOCKER_BUILDKIT=0使用仍可用的旧构建器限额，不修改全局Docker设置。旧构建器已弃用，未来可换专用受限BuildKit/CI构建，不需要更换应用架构。

DSH打包运行时需要从临时目录加载随包原生模块。Agent保留/tmp为noexec，仅新增64MiB、UID/GID1000、0700、nosuid/nodev的/native-cache执行临时区，通过TMPDIR指定。它不提供Shell工具、真实Gateway或宿主挂载权限，重启即清空，使用量计入2GiB内存上限。参见[Docker tmpfs选项](https://docs.docker.com/engine/storage/tmpfs)。不能把只读根文件系统解释为整个容器完全没有可写区域。

## 显式准备和启动

只在新clone目录执行，不在服务器现有桌面项目中操作。主机需要Docker权限、UID1000、Python3和足够资源；不安装系统Python/Node依赖。

```bash
cd agent-service/dsh-prototype
python3 tools/isolated_setup.py
# .isolated为新建0700目录，内含0600令牌/配置；已有目录拒绝覆盖。
```

分别构建deploy/Dockerfile的agent、frontend、access三个target，对应镜像名dsh-test-agent:stage2、dsh-test-management:stage2、dsh-test-access:stage2。运行环境统一指定文件和项目名：

当前服务器可用的显式限额构建入口如下；代理变量按本机实际配置，仅对当前命令生效，不应把认证信息写入命令历史或Git。默认构建网络为default；只有可信构建步骤确需访问本机代理时才选host：

```bash
python3 tools/build_isolated.py --legacy-limited --network host
```

脚本在低内存/磁盘余量或较高负载时拒绝，逐个构建并保留有时间标识的私有日志，不自动清理。新建环境应使用明确提交作为--tag，并在.isolated/deployment.env记录DSH_TEST_IMAGE_TAG；之后Compose加--env-file .isolated/deployment.env。环境验收脚本自动使用该本机文件。镜像tag不是安全签名，实际镜像ID和源码哈希仍要一同记录。

```bash
docker compose -p dsh-test -f compose.isolated.yaml --profile setup run --rm --no-deps initialize
docker compose -p dsh-test -f compose.isolated.yaml up -d --no-build --wait fake-provider agent management access
```

initialize只给空的专用state创建新虚构库，并显式建v5结构；serve缺库/版本不符/配置变化拒绝，不迁移已有库。两个虚构身份仅在本测试模块有效，与Server平台账号无关。令牌不输出、不进入镜像/日志/Git；管理员能力通过只读secret文件提供。

## 存活、就绪和控制

- `/health`：只读内存状态，返回200表示进程能应答，不表示模型质量或准入就绪。
- `/ready`：每次重启后503 paused。只读本地状态，不探测模型、查历史或暖机。
- 容器healthcheck使用/health，便于管理员在暂停时也能打开界面核对。
- 显式放行命令先读取固定假提供方的活动计数（不调用模型），然后复用原verify_ready；存在待对账操作仍拒绝，不重跑或丢弃数据。

```bash
docker compose -p dsh-test -f compose.isolated.yaml exec -T agent python -m dsh_tutor.isolated_host verify
```

控制通过0600 Unix socket及SO_PEERCRED核对容器UID，只有一个verify命令。学生HTTP没有迁移、清库、恢复准入或任意执行入口。未知状态仍保持暂停，后续需可信操作者按已有对账接口核实；本阶段不做自动恢复。

## 环境验收与访问

首次启动、保持暂停、无其他测试任务时执行：

```bash
python3 tools/isolated_acceptance.py
```

该命令只操作当前目录标签匹配的dsh-test项目，检查权限/资源、无令牌拒绝、只读探针、一例真实DSH到假提供方的双保存问答、重发去重、学生隔离、管理投影，并重启一次测试Agent确认持久结果及重新暂停。不会清空数据，不能并发重复运行。报告在被忽略的.isolated/environment-report.json。重复验收前应先停止其他测试并明确将Agent重启为暂停状态，不以删除数据库复跑。

从自己的电脑打开SSH转发后访问管理页（不是直接访问服务器IP高端口）：

```bash
ssh -N -L 13002:127.0.0.1:13002 arclab@服务器地址
```

浏览器打开http://127.0.0.1:13002。管理会话以可信本机访问为边界，不等于正式平台管理员登录，不能直接公开。读管理接口不会调用模型或展示学生正文；固定假提供方没有保存请求正文，只保留计数。

## 停止和撤回

```bash
docker compose -p dsh-test -f compose.isolated.yaml stop
```

只停止这套测试容器，保留专用数据。不要执行根Compose、全局docker prune、删除通配目录或down -v。私有数据的备份/删除和远端未知状态单独核实；停止本地容器不应被说成已经取消任意远端模型。
