# DSH 版平台 Agent（端口 8770，接口与 main 的 agent-service 相同）。
# 构建：docker build -f deploy/Dockerfile .（或用 compose.platform.yaml）
# DSH 运行时是 deepseek-harness-runtime-bin 里的单个可执行文件，不需要 Node。
FROM python:3.10.12-slim-bookworm@sha256:4d440b214e447deddc0a94de23a3d97d28dfafdf125a8b4bb8073381510c9ee2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 dsh-runner && useradd -u 1000 -g 1000 -m dsh-runner
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 HOME=/home/dsh-runner \
    TMPDIR=/native-cache AIVIRTEACH_DSH_STATE_DIR=/native-cache/runs \
    DSH_TELEMETRY_DISABLED=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt && python -m pip check
COPY dsh-base.patch.yml ./
COPY dsh_tutor/ ./dsh_tutor/
USER 1000:1000
EXPOSE 8770
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD ["python","-c","import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/health', timeout=3).read()"]
CMD ["sh","-c","exec python -m uvicorn dsh_tutor.platform_app:app --host \"${AIVIRTEACH_AGENT_HOST:-0.0.0.0}\" --port \"${AIVIRTEACH_AGENT_PORT:-8770}\" --no-access-log"]
