# 精确解释器/Node版本与现有回归基线一致；构建后记录实际镜像digest。
FROM node:22.13.0-bookworm-slim@sha256:f5a0871ab03b035c58bdb3007c3d177b001c2145c18e81817b71624dcf7d8bff AS node-runtime

FROM python:3.10.12-slim-bookworm@sha256:4d440b214e447deddc0a94de23a3d97d28dfafdf125a8b4bb8073381510c9ee2 AS agent
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 dsh-runner && useradd -u 1000 -g 1000 -m dsh-runner
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 HOME=/home/dsh-runner
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt && python -m pip check
COPY dsh_tutor/ ./dsh_tutor/
COPY config/runtime.example.json ./config/runtime.example.json
COPY ollama.patch.yml ./
COPY sandbox/__init__.py sandbox/isolated_provider.py ./sandbox/
COPY tools/isolated_health.py ./tools/isolated_health.py
USER 1000:1000
CMD ["python","-m","dsh_tutor.isolated_host","serve"]

FROM node-runtime AS frontend-build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /build
COPY management_frontend/package.json management_frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY management_frontend/app ./app
COPY management_frontend/tsconfig.json management_frontend/next-env.d.ts management_frontend/next.config.ts ./
RUN npm run typecheck && npm run build

FROM node-runtime AS frontend
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=13002 \
    DSH_MANAGEMENT_PORT=13002 DSH_MANAGEMENT_BASE_URL=http://agent:18788
WORKDIR /app
COPY --from=frontend-build --chown=node:node /build/.next/standalone ./
COPY --from=frontend-build --chown=node:node /build/.next/static ./.next/static
COPY --chown=node:node deploy/frontend-entry.cjs ./frontend-entry.cjs
USER 1000:1000
CMD ["node","frontend-entry.cjs"]

FROM nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284 AS access
COPY deploy/nginx-isolated.conf /etc/nginx/nginx-isolated.conf
USER 1000:1000
ENTRYPOINT ["nginx"]
CMD ["-c","/etc/nginx/nginx-isolated.conf","-g","daemon off;"]
