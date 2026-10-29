FROM ghcr.io/astral-sh/uv:0.12.3-python3.13-trixie-slim@sha256:5f3c58899cb4ab5b723f81641d6aed08968e6c93f9a84641321ae66ba7103f42

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBVIRT_DEFAULT_URI=qemu:///system

COPY requirements.txt ./requirements.txt

COPY diagnostic-gateway/gateway_service.py ./gateway_service.py
COPY diagnostic-gateway/diagnostic_gateway.py ./diagnostic_gateway.py

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libvirt-clients \
    && rm -rf /var/lib/apt/lists/* \
    && uv pip install --system --no-cache --requirement requirements.txt

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "gateway_service:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
