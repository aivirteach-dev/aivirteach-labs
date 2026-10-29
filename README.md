# aivirteach-labs

Labs 负责执行和可靠投递；课程进度的 canonical state 仍只属于 `aivirteach-server`。

## Agent 课程文件

原始课程保存在 `agent-service/.cache/course/AI Daily Briefing/raw`。运行下面的命令会把 Markdown、检查点、常见故障和文本附件转换为适合 Agent 检索的结构化 JSON：

```bash
.venv/bin/python agent-service/scripts/process_course.py
```

生成结果：

```text
agent-service/.cache/course/AI Daily Briefing/processed/course.agent.json
agent-service/.cache/course/AI Daily Briefing/processed/index.json
```

Agent 默认从该 `processed` 目录检索。部署到其他位置时设置：

```bash
export AIVIRTEACH_COURSE_DIR="/path/to/processed"
```

`aivirteach-server` 仍负责传入当前 `course_id` 和 `lesson_id`；Agent 只用本地课程文件补全步骤说明、预期结果、成功标准、常见故障和相关原文，不会从课程文件扩大诊断工具权限。
重新生成课程文件或修改 `AIVIRTEACH_COURSE_DIR` 后，需要重启 Agent 服务以重新加载课程索引。

使用默认开启深度思考的 OpenAI-compatible 模型时，如果当前 Agent 适配器不需要传递模型的思考过程，可设置：

```bash
export AIVIRTEACH_MODEL_THINKING="disabled"
```

未设置该变量时不会向其他 OpenAI-compatible 服务发送供应商专用参数。

## FastAPI 服务

四个端口各自运行独立的应用，另有一个不监听端口的 Progress Worker。这样高权限 VM 管理、只读调查、模型推理、可靠投递和文档展示不会共用同一个进程：

| 端口 | 服务 | 实际职责 |
| --- | --- | --- |
| 8760 | VM Manager | 只执行 VM 管理并提供自身 OpenAPI |
| 8765 | Diagnostic Gateway | 执行固定模板的只读调查工具 |
| 8770 | Agent Service | 结合课程上下文推理并调用 8765 |
| 8780 | Unified Docs Service | 聚合三个服务的 OpenAPI，不执行服务操作 |

`progress-worker/` 没有 HTTP 端口，也不会加入 8780 Swagger。它从 Server 取得可信 VM/课程目标，经 8765 检测，并用本地 SQLite outbox 把规范化事件幂等回传 Server。

在 `http://127.0.0.1:8780/docs` 使用 **Try it out** 时，Swagger 浏览器会按照每个操作声明的地址直接请求 8760、8765 或 8770；8780 不代理这些操作。

### 安装依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 启动

先在终端 1 启动只读 Diagnostic Gateway：

当前用户需要能访问 `qemu:///system`；Ubuntu 本地开发通常将用户加入
`libvirt` 组并重新登录即可，不要求配置免密 sudo。

```bash
export AIVIRTEACH_DIAGNOSTIC_TOKEN="$(openssl rand -hex 32)"
export AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN="$(openssl rand -hex 32)"
export AIVIRTEACH_DIAGNOSTIC_PORT="8765"
export AIVIRTEACH_DIAGNOSTIC_CORS_ORIGINS="http://127.0.0.1:8780,http://localhost:8780"
./diagnostic-gateway/start_diagnostic_service.sh
```

在终端 2 启动 VM Manager：

```bash
export AIVIRTEACH_API_TOKEN="$(openssl rand -hex 32)"
sudo --preserve-env=AIVIRTEACH_API_TOKEN \
  ./vm-manager/start_service.sh
```

在终端 3 启动 Agent Service。它使用与 Diagnostic Gateway 相同的诊断 token，并使用独立的 Agent token：

```bash
export AIVIRTEACH_AGENT_TOKEN="$(openssl rand -hex 32)"
export AIVIRTEACH_DIAGNOSTIC_TOKEN="与 Diagnostic Gateway 完全相同的 token"
export AIVIRTEACH_GATEWAY_URL="http://127.0.0.1:8765"
export AIVIRTEACH_MODEL_PROVIDER="fake"
./agent-service/start_agent_service.sh
```

也可以从仓库根目录使用统一 Compose 运行 Diagnostic Gateway、Agent 和
Unified Docs：

```bash
mkdir -p "agent-service/.cache/course"
docker compose --env-file config.env up -d --build \
  diagnostic-service agent-service unified-docs
docker compose --env-file config.env ps
curl http://127.0.0.1:8765/ready
curl http://127.0.0.1:8770/ready
curl http://127.0.0.1:8780/ready
```

根目录 `config.env` 需要包含本页后文 Compose 清单列出的所有必需密钥，因为
Compose 会在选择服务前解析整份文件；其中 Agent 与 Diagnostic Gateway 使用
`AIVIRTEACH_DIAGNOSTIC_TOKEN`、`AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN` 和
`AIVIRTEACH_AGENT_TOKEN`。Agent 与 Gateway 必须使用同一枚
`AIVIRTEACH_DIAGNOSTIC_TOKEN`。默认使用 `fake` provider；
实际模型还要配置 `AIVIRTEACH_MODEL_PROVIDER=openai_compatible`、模型 URL、API Key
和模型名称。容器内的 Gateway URL 已固定为 Compose 服务地址
`http://diagnostic-service:8765`。课程缓存默认从宿主机
`agent-service/.cache/course` 只读挂载，可用
`AIVIRTEACH_COURSE_CACHE_HOST` 改为其他宿主机目录。

在终端 4 启动不带任何服务 Token 的 Unified Docs Service：

```bash
./docs-service/start_docs_service.sh
```

如果上一段已经启动 Compose 的 `unified-docs`，不要再运行这个主机脚本；统一
Swagger 页面直接访问 `http://127.0.0.1:8780/docs`。

默认情况下，容器从 `vm-manager:8760`、`diagnostic-service:8765` 和
`agent-service:8770` 获取 OpenAPI。如果三个运行时服务仍由宿主机脚本启动，可在
`config.env` 中把相应的 `AIVIRTEACH_*_OPENAPI_URL` 改为
`http://host.docker.internal:<端口>/openapi.json`；Compose 已为 Linux 配置
`host-gateway` 映射。

在 Server 已启动后，可在终端 5 验证并启动 Progress Worker。它只拿 `AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN`，因此不能调用 journal、文件、容器或网络诊断；同时另持有一枚绑定本机 `worker_id` 的 Server token：

```bash
cp progress-worker/config/progress.env.example \
  progress-worker/config/progress.env.local
chmod 600 progress-worker/config/progress.env.local
# 编辑文件，使 AIVIRTEACH_PROGRESS_SERVER_TOKEN 与 Server
# PROGRESS_WORKER_TOKENS 中 labs-host-01 对应的值相同；两个 token
# 都必须与 Agent 使用的 AIVIRTEACH_DIAGNOSTIC_TOKEN 不同。
set -a
source progress-worker/config/progress.env.local
set +a
./progress-worker/start_progress_worker.sh --once
./progress-worker/start_progress_worker.sh
```

生产环境使用 `systemd/aivirteach-progress-worker.service` 和 `/etc/aivirteach-progress.env`。Worker 账户不需要 root、`libvirt` 组、libvirt socket 或 VM 镜像目录权限。

文档地址：

```text
http://127.0.0.1:8780/docs  # 三个服务的统一文档（Swagger 资源本地托管）
http://127.0.0.1:8760/docs  # VM Manager 自身文档
http://127.0.0.1:8765/docs  # Diagnostic Gateway 自身文档
http://127.0.0.1:8770/docs  # Agent Service 自身文档
```

### 接口

```text
GET    /health
GET    /v1/vms                              # 列出 lab_id、VM UUID 和状态
POST   /v1/vms
GET    /v1/vms/{lab_id}/status
GET    /v1/vms/{lab_id}/ip
GET    /v1/vms/{lab_id}/vnc
GET    /v1/vms/{lab_id}/credentials
POST   /v1/vms/{lab_id}/browser-sessions
POST   /v1/vms/{lab_id}/actions/{start|stop|force-stop|reboot}
DELETE /v1/vms/{lab_id}?confirm=true

POST   /v1/diagnostics/{lab_id}/tools/{tool}  # 实际运行于 8765
POST   /v1/agent/diagnose                     # 实际运行于 8770
POST   /v1/agent/diagnose/stream              # 8770 SSE 进度、回答增量与最终结果
```

Agent 的普通 JSON 和 SSE 接口使用同一条诊断与回答流程。第一阶段输出
`DiagnosticAssessment`，包含诊断结论、推测、证据引用、建议与限制，不包含完整
`answer`。结构和证据引用通过校验后，模型只生成一次学员正文。校验不证明模型推测
为事实。若校验失败，返回通用重试提示，不把原始模型 JSON 作为答案；若正文生成
失败，使用已校验诊断的确定性摘要兜底，不再调用模型重写。

SSE 使用与普通诊断接口相同的 JSON request body 和 `AIVIRTEACH_AGENT_TOKEN`。
正常流程依次发送 `accepted`、`started`、`context_ready`、推理与工具状态、
`assessment_validating`、`assessment_validated`、`answer_started`、
`assistant_delta`、`answer_finished`、`result` 和 `done`。每个 delta 可包含多个
字符或 token。诊断无效时不会产生正文增量，而是发送 `answer_failed` 和最终
`result`。正文中断时客户端应清除草稿，并以 `result.response.answer` 替换。

`result.response` 保持原有 `DiagnoseResponse` 契约；普通接口等待同一结果后返回
JSON。两种接口的用量均包含诊断调用和最终正文调用。Labs 的 `answer_finished`
只表示生成结束，不表示 Server 已保存消息。接口不发送模型内部推理、工具参数或
原始日志。浏览器 POST 应使用 `fetch` 的 ReadableStream，而不是只发 GET 的
`EventSource`。

终端测试时，把普通 Agent request body 保存为 `request.json`：

```bash
curl -N -X POST \
  -H "Authorization: Bearer $AIVIRTEACH_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @request.json \
  http://127.0.0.1:8770/v1/agent/diagnose/stream
```

课程进度工具的只读测试：

```bash
curl -X POST \
  -H "Authorization: Bearer $AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"vm_instance_id\":\"$(virsh --connect qemu:///system domuuid lab-001)\",\"parameters\":{\"checkpoint_ids\":[\"P01\"]}}" \
  http://127.0.0.1:8765/v1/diagnostics/lab-001/tools/check_course_progress
```

请求必须带当前 libvirt `vm_instance_id`，且 parameters 只能包含 P01–P24 的 `checkpoint_ids`；Gateway 会先核对名称与 UUID，再固定执行镜像内的 `/usr/local/bin/aivirteach-check-progress`，不接受 path、argv 或 shell。P01–P07 是当前镜像的 guest probes；P08–P24 仍需 n8n execution、浏览器或邮件回执等平台证据。

Swagger 右上角的 **Authorize** 会分别显示 Admin、Session、Diagnostic 和 Agent Bearer Token。

调用示例：

```bash
curl -H "Authorization: Bearer $AIVIRTEACH_API_TOKEN" \
  http://127.0.0.1:8760/v1/vms/lab-001/ip
```

在 VM Manager 或 Unified Docs 中查看全部 VM ID：

```bash
curl -H "Authorization: Bearer $AIVIRTEACH_API_TOKEN" \
  http://127.0.0.1:8760/v1/vms
```

响应同时提供 Labs 使用的 `lab_id`、防止同名 VM 重建混淆的不可变
`vm_instance_id`（libvirt UUID），以及当前运行状态。

只读诊断调用示例：

```bash
curl -X POST -H "Authorization: Bearer $AIVIRTEACH_DIAGNOSTIC_TOKEN" \
  -H "Content-Type: application/json" -d '{"parameters":{}}' \
  http://127.0.0.1:8765/v1/diagnostics/lab-001/tools/get_vm_status
```

三个运行时服务默认只允许来自 `http://127.0.0.1:8780` 和 `http://localhost:8780` 的统一文档页面跨端口请求。部署到其他域名时，通过各服务的 `AIVIRTEACH_*_CORS_ORIGINS` 设置精确来源，不要使用通配符。

这里的 CORS 只作用于 8760/8765/8770 的 FastAPI HTTP 接口，主要用于统一 Swagger 页面跨端口执行请求。它不是 Guacamole 8080 的 iframe/WebSocket 配置。正常学习链路由 `aivirteach-server` 在服务端调用 8760，不受浏览器 CORS 限制，因此不要为了远程桌面把 Client 公网 Origin 加进 `AIVIRTEACH_VM_CORS_ORIGINS`，更不要设置成 `*`。

远程开发访问建议保留 loopback 监听，并同时转发文档端口和诊断端口：

```bash
ssh -N \
  -L 18760:127.0.0.1:8760 \
  -L 18765:127.0.0.1:8765 \
  -L 18770:127.0.0.1:8770 \
  -L 18780:127.0.0.1:8780 \
  your-user@LIBVIRT_HOST
```

使用上述本地转发端口时，Docs Service 仍从主机内部的 8760/8765/8770 获取 schema，但需要把 Swagger 操作地址改成浏览器实际可达的转发地址：

- 8780：`AIVIRTEACH_VM_DOCS_URL=http://127.0.0.1:18760`、`AIVIRTEACH_DIAGNOSTIC_DOCS_URL=http://127.0.0.1:18765`、`AIVIRTEACH_AGENT_DOCS_URL=http://127.0.0.1:18770`
- 8760：`AIVIRTEACH_VM_CORS_ORIGINS=http://127.0.0.1:18780`
- 8765：`AIVIRTEACH_DIAGNOSTIC_CORS_ORIGINS=http://127.0.0.1:18780`
- 8770：`AIVIRTEACH_AGENT_CORS_ORIGINS=http://127.0.0.1:18780`

生产环境应使用 HTTPS 反向代理和防火墙。环境文件分别是 `/etc/aivirteach-labs.env`、`/etc/aivirteach-diagnostics.env`、`/etc/aivirteach-agent.env`、`/etc/aivirteach-progress.env` 和 `/etc/aivirteach-docs.env`。

## 浏览器内嵌 RDP（Guacamole）

### 当前架构决定

当前 `local-demo` / `vm_agent_local` 代码统一采用同源 Guacamole 路径，不使用旧 PR 中的 `IronRDP + websockify + labs-console.<domain>` 浏览器直连方案：

```text
Browser / Tauri
  └─ https://learn.<domain>/workspace
      ├─ POST /api/v1/me/lab/session
      └─ /guacamole/?data=<短期不透明票据>
           └─ 同源反向代理 / Cloudflare Tunnel
               └─ Guacamole Web :8080
                   └─ guacd :4822
                       └─ VM :3389
```

Server 返回相对地址 `/guacamole/?data=...`，Client 也只接受这个相对同源合约。当前部署不需要 `LABS_CONSOLE_WS_URL`、websockify 或公开的 `labs-console.<domain>`。如果旧分支仍需要这些组件，应单独维护旧分支文档，不要与这里的 Guacamole 配置混装。

仓库根目录的 `compose.yaml` 会启动 VM Manager（8760）、Diagnostic Gateway
（8765）、Agent Service（8770）、Unified Docs（8780）、Guacamole Web（8080）
和 guacd；所有宿主机 HTTP 端口都只绑定到 loopback。Docker 版 VM Manager 不是在容器里运行嵌套虚拟机：它通过只挂载进容器的宿主机 `/var/run/libvirt/libvirt-sock` 连接宿主机 `qemu:///system`，所以 API 创建出来的 domain 会出现在宿主机的 `virsh --connect qemu:///system list --all` 中。

为了让 `POST /v1/vms` 真正可用，容器镜像包含 `qemu-img`、`cloud-localds` 和 `virt-install`，并把下面两个宿主机目录以相同绝对路径读写挂载：

```text
/var/lib/libvirt/images/aivirteach  VM golden image、overlay 和 cloud-init seed
/var/lib/aivirteach-labs           VM 凭据和运行状态
```

容器还只额外保留 `CHOWN`、`FOWNER` capability，并只读挂载宿主机 `/etc/passwd`、`/etc/group`，让脚本生成的磁盘保持宿主机 `libvirt-qemu:kvm` 所需的实际 UID/GID。因为“libvirt socket + 可写 VM 存储”相当于把宿主机虚拟化管理权交给该容器，8760 必须保持 loopback/私网并使用强管理 Token，不能把管理员 API 直接暴露到公网。

首次启动时，在仓库根目录创建不提交 Git 的 `config.env`，配置下列必需值：

```bash
touch config.env
chmod 600 config.env
mkdir -p "agent-service/.cache/course"
```

局域网访问地址统一由 `AIVIRTEACH_HOST_IP` 管理。CORS 和 Swagger 的浏览器可见
地址应通过 `${AIVIRTEACH_HOST_IP}` 派生；迁移服务器时只修改这一行：

```dotenv
AIVIRTEACH_HOST_IP="10.162.179.63"
AIVIRTEACH_VM_CORS_ORIGINS="http://${AIVIRTEACH_HOST_IP}:8780"
AIVIRTEACH_DIAGNOSTIC_CORS_ORIGINS="http://${AIVIRTEACH_HOST_IP}:8780"
AIVIRTEACH_AGENT_CORS_ORIGINS="http://${AIVIRTEACH_HOST_IP}:8780"
AIVIRTEACH_VM_DOCS_URL="http://${AIVIRTEACH_HOST_IP}:8760"
AIVIRTEACH_DIAGNOSTIC_DOCS_URL="http://${AIVIRTEACH_HOST_IP}:8765"
AIVIRTEACH_AGENT_DOCS_URL="http://${AIVIRTEACH_HOST_IP}:8770"
```

分别生成 JSON 认证密钥和各服务 token：

```bash
openssl rand -hex 16  # GUACAMOLE_JSON_SECRET（也兼容 AIVIRTEACH_GUACAMOLE_JSON_SECRET）
openssl rand -hex 32  # AIVIRTEACH_SESSION_TOKEN
openssl rand -hex 32  # AIVIRTEACH_API_TOKEN
openssl rand -hex 32  # AIVIRTEACH_DIAGNOSTIC_TOKEN
openssl rand -hex 32  # AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN
openssl rand -hex 32  # AIVIRTEACH_AGENT_TOKEN
```

`AIVIRTEACH_SESSION_TOKEN` 必须与 `aivirteach-server/.env` 中的 `LABS_SESSION_TOKEN` 完全一致。然后启动：

```bash
docker compose --env-file config.env up -d --build
docker compose --env-file config.env ps
```

Docker 版启动后不要同时运行 `vm-manager/start_service.sh`、
`diagnostic-gateway/start_diagnostic_service.sh` 或
`agent-service/start_agent_service.sh`、`docs-service/start_docs_service.sh`，否则会
争用 8760、8765、8770 或 8780。先验证容器确实连到宿主机 libvirt，并具备创建工具和写入目录：

```bash
docker compose --env-file config.env exec vm-manager \
  virsh --connect qemu:///system list --all
docker compose --env-file config.env exec vm-manager sh -lc \
  'command -v qemu-img && command -v cloud-localds && command -v virt-install'
docker compose --env-file config.env exec vm-manager \
  test -w /var/lib/libvirt/images/aivirteach/labs
docker compose --env-file config.env exec vm-manager \
  test -w /var/lib/aivirteach-labs
```

调用创建接口后，在宿主机确认 VM，而不是进入容器寻找 QEMU 进程：

```bash
curl -X POST http://127.0.0.1:8760/v1/vms \
  -H "Authorization: Bearer $AIVIRTEACH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"lab_id":"lab-003","memory_mb":4096,"vcpus":2,"autostart":false}'

sudo virsh --connect qemu:///system dominfo lab-003
```

开发环境由 client 将同源路径 `/guacamole` 代理到 `http://127.0.0.1:8080`。server 只把 learner 映射到可信 `lab_id`，浏览器不会收到虚拟机明文 IP 或 RDP 密码。

Tauri 桌面版也使用同一条 Guacamole 链路：桌面壳加载已部署的 React Workspace，生产入口必须在同一个 HTTPS Origin 下把 `/guacamole/` 反向代理到这里的 `127.0.0.1:8080/guacamole/`，并保留 HTTP/1.1 WebSocket Upgrade、关闭 buffering、设置长连接 timeout。不要公开 8760、guacd 4822 或 VM 3389，也不要把 `LABS_SESSION_TOKEN` 或 `GUACAMOLE_JSON_SECRET` 打进桌面安装包。

### Cloudflare Tunnel 开放方式

推荐只给用户开放一个 HTTPS hostname，例如 `learn.example.com`。让该 hostname 先到达同源 gateway（例如 Labs 主机上的 Nginx），再由 gateway 按 path 分发：

```text
learn.example.com
  └─ Cloudflare Tunnel → http://127.0.0.1:8090
      ├─ /                  → React Client
      ├─ /api/v1/           → AIVirTeach Server
      └─ /guacamole/        → http://127.0.0.1:8080/guacamole/
```

本地管理 Tunnel 的最小 `/etc/cloudflared/config.yml` 示例：

```yaml
tunnel: <TUNNEL_UUID>
credentials-file: /etc/cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: learn.example.com
    service: http://127.0.0.1:8090
  - service: http_status:404
```

然后为 hostname 建 DNS route 并启动 Tunnel：

```bash
cloudflared tunnel route dns <TUNNEL_UUID或名称> learn.example.com
cloudflared tunnel ingress validate
sudo systemctl enable --now cloudflared
```

Cloudflare 也推荐通过 Dashboard 创建 remotely-managed Tunnel；无论采用哪种管理方式，公开路由都应指向同源 gateway，而不是重新开放旧的 `labs-console.<domain>:6080`。Cloudflare Tunnel 支持把公网 hostname 映射到本地 HTTP 服务，并透明承载 HTTP WebSocket；gateway 仍必须保留 `Upgrade`/`Connection` 请求头、关闭 buffering 并配置长连接 timeout。参考 [Cloudflare Tunnel Routing](https://developers.cloudflare.com/tunnel/routing/) 和 [本地配置文件说明](https://developers.cloudflare.com/tunnel/advanced/local-management/configuration-file/)。

如果 React/Server 部署在 Vercel、Cloudflare Pages 或其他主机，那个环境的 `127.0.0.1:8080` 不是 Labs。此时应由 `learn.example.com` 前面的 edge/path router 把 `/guacamole/` 转发到 Labs Tunnel origin；浏览器看到的地址仍然必须是 `https://learn.example.com/guacamole/...`。可以给 Server-to-Labs 的 8760 另建受 Cloudflare Access Service Token 保护的私有 hostname，但浏览器 `/guacamole/` 路径不能依赖需要暴露机器 Service Token 的认证方式。

公网入口不要暴露以下端口：

```text
8760  VM Manager
4822  guacd
3389  VM RDP
```

Guacamole iframe 通常不依赖 VM Manager CORS；需要检查的是反向代理 WebSocket、CSP `frame-src`、响应 `frame-ancestors` 和 `X-Frame-Options`。外部联调时，在浏览器 Network 中确认 Guacamole tunnel 成功升级为 HTTP 101。

安全检查：当前 Compose 配置还包含浏览器会话以外的高权限 Token 注入，且旧版本可能曾把值直接写进受版本控制文件。生产重启前要先确认哪些管理接口仍在使用它们，把需要保留的值迁移到不提交 Git 的 `.env`/secret store，并轮换所有曾进入 Git 或日志的 Token。不要只删除文件中的一行就假设旧值已失效。

## 查看虚拟机日志

`vm-manager/libvirt/scripts/vm-logs.sh` 用于读取指定虚拟机在宿主机上的 QEMU/libvirt 日志或相关 systemd journal。它不会进入虚拟机内部读取 guest OS 日志。

查看最近 200 行 QEMU 日志：

```bash
./vm-manager/libvirt/scripts/vm-logs.sh lab-001
```

指定行数：

```bash
./vm-manager/libvirt/scripts/vm-logs.sh lab-001 --lines 500
```

查看宿主机 journal 中包含该 VM ID 的记录：

```bash
./vm-manager/libvirt/scripts/vm-logs.sh lab-001 --source journal
```

同时查看两类日志：

```bash
./vm-manager/libvirt/scripts/vm-logs.sh lab-001 --source all --lines 300
```

持续跟踪新日志，使用 `Ctrl+C` 停止：

```bash
./vm-manager/libvirt/scripts/vm-logs.sh lab-001 --source qemu --follow
./vm-manager/libvirt/scripts/vm-logs.sh lab-001 --source journal --follow
```

## 服务文件分布

仓库根目录只保留各 Python 服务与 Worker 共用的依赖、部署文件和测试；每个边界的入口、实现、专属配置及资源放在自己的目录中。

```text
aivirteach-labs/
├── vm-manager/                 # 8760：VM 管理
│   ├── start_service.sh        # 本地/systemd 启动入口
│   ├── service.py              # VM 生命周期与浏览器 RDP 会话
│   ├── config/                 # VM Manager 环境变量示例
│   ├── libvirt/                # VM 镜像、创建、控制与日志脚本
│   └── guacamole/              # 浏览器内嵌 RDP 的 Compose 配置
├── diagnostic-gateway/         # 8765：特权、只读诊断网关
│   ├── start_diagnostic_service.sh
│   ├── gateway_service.py      # FastAPI 应用入口、CORS、健康检查
│   ├── diagnostic_gateway.py   # 固定诊断工具与 QEMU Guest Agent 调用
│   └── config/                 # Diagnostic 环境变量示例
├── agent-service/              # 8770：课程感知的排障 Agent
│   ├── start_agent_service.sh
│   ├── agent_service.py        # FastAPI 应用入口
│   ├── aivirteach_agent/       # 编排、工具策略、模型适配与课程检索
│   ├── scripts/                # 课程转换工具
│   ├── config/                 # Agent 环境变量示例
│   └── .cache/course/          # 原始和处理后的课程文件（不提交 Git）
├── docs-service/               # 8780：无特权统一 API 文档
│   ├── start_docs_service.sh
│   ├── unified-docs.Dockerfile # 非 root、只读根文件系统的容器入口
│   ├── docs_service.py         # 文档页面、健康检查和 OpenAPI 端点
│   ├── openapi_aggregator.py   # 通过 HTTP 获取并合并三个服务 schema
│   ├── static/                 # 本地 Swagger UI JS/CSS/图标
│   └── config/                 # Docs Service 环境变量示例
├── progress-worker/            # 无端口：检测、SQLite outbox、可靠投递
│   ├── start_progress_worker.sh
│   ├── progress_worker/        # 配置、严格 wire models、HTTP client、store、scheduler
│   └── config/                 # Worker 环境变量示例
├── systemd/                    # 四个 HTTP 服务和 Progress Worker 的 unit
├── tests/                      # 跨服务 Python 和 Shell 测试
└── requirements.txt            # Python 服务与 Worker 共享依赖
```

| 服务 | 入口 | 环境变量示例 | 主要专属依赖 |
| --- | --- | --- | --- |
| VM Manager | `vm-manager/start_service.sh` | `vm-manager/config/api.env.example` | `vm-manager/libvirt/`、`vm-manager/guacamole/` |
| Diagnostic Gateway | `diagnostic-gateway/start_diagnostic_service.sh` | `diagnostic-gateway/config/diagnostics.env.example` | libvirt socket、QEMU Guest Agent |
| Agent Service | `agent-service/start_agent_service.sh` | `agent-service/config/agent.env.example` | `agent-service/aivirteach_agent/`、`agent-service/.cache/course/` |
| Unified Docs | `docs-service/start_docs_service.sh` / `unified-docs` Compose service | `docs-service/config/docs.env.example` | 三个服务的 `/openapi.json`、本地 Swagger UI 静态资源 |
| Progress Worker | `progress-worker/start_progress_worker.sh` | `progress-worker/config/progress.env.example` | 8765、Server internal API、SQLite WAL/outbox |

服务间调用方向：

```text
aivirteach-server ──► Agent Service :8770 ──► Diagnostic Gateway :8765 ──► VM

aivirteach-server ──► VM Manager :8760 ──► libvirt

aivirteach-server ──targets──► Progress Worker ──► Diagnostic Gateway :8765 ──► VM
       ▲                         │
       └────idempotent events───┴── SQLite outbox

Docs Service :8780 ──HTTP GET──► :8760/:8765/:8770 /openapi.json
```

Docs Service 不 import 三个运行时服务的 Python 实现，也不持有服务 Token。某个服务暂时离线时，它会继续展示上一次成功读取的 schema 并标记为 `stale`；从未读取成功的服务会标记为 `unavailable`。

Progress Worker 也不 import VM Manager 或 Agent。Server 按 `event_id` 去重；只有 Server 回应的 `accepted_event_ids` 会在本地标为 delivered。网络中断、Server 重启或 Worker 在发送后崩溃都只会造成同一 ID 重投，不会凭空增加完成步骤。Worker 将 Server 下发的预期 UUID 交给 Gateway；Gateway 在 QGA 前核对 `lab_id` 的实际 libvirt UUID、通过该 UUID 执行，并在响应中回传给 Worker再次核对。VM 删除重建时必须在 Server assignment 中换成新 UUID，旧 outbox 因而不能污染同名 `lab_id`。
