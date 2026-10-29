FROM ghcr.io/astral-sh/uv:0.12.3-python3.13-trixie-slim@sha256:5f3c58899cb4ab5b723f81641d6aed08968e6c93f9a84641321ae66ba7103f42

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

COPY requirements.txt ./requirements.txt

COPY docs-service/docs_service.py ./docs_service.py
COPY docs-service/openapi_aggregator.py ./openapi_aggregator.py
COPY docs-service/static ./static

RUN uv pip install --system --no-cache --requirement requirements.txt

RUN chown -R 65532:65532 /app

USER 65532:65532

EXPOSE 8780

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/health', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "docs_service:app", "--host", "0.0.0.0", "--port", "8780", "--no-access-log"]
