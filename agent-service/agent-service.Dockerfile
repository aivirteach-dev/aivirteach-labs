FROM ghcr.io/astral-sh/uv:0.12.3-python3.13-trixie-slim@sha256:5f3c58899cb4ab5b723f81641d6aed08968e6c93f9a84641321ae66ba7103f42

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

COPY requirements.txt ./requirements.txt

COPY agent-service/agent_service.py ./agent_service.py
COPY agent-service/aivirteach_agent ./aivirteach_agent

RUN uv pip install --system --no-cache --requirement requirements.txt

# Keep an empty course path in the image so readiness works before the optional
# host cache is populated. Compose overlays /app/.cache/course read-only.
RUN mkdir -p "/app/.cache/course/AI Daily Briefing/processed" \
    && chown -R 65532:65532 /app

USER 65532:65532

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/health', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "agent_service:app", "--host", "0.0.0.0", "--port", "8770", "--no-access-log"]
