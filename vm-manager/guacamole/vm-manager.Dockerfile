FROM ghcr.io/astral-sh/uv:0.12.3-python3.13-trixie-slim@sha256:5f3c58899cb4ab5b723f81641d6aed08968e6c93f9a84641321ae66ba7103f42

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY vm-manager/service.py ./service.py
COPY vm-manager/libvirt/config ./libvirt/config
COPY vm-manager/libvirt/scripts ./libvirt/scripts
COPY vm-manager/guacamole/virt-install-wrapper.sh /usr/local/bin/virt-install

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        acl \
        cloud-image-utils \
        libosinfo-bin \
        libvirt-clients \
        openssl \
        qemu-utils \
        virtinst \
    && rm -rf /var/lib/apt/lists/*

RUN uv pip install --system --no-cache --requirement requirements.txt

RUN chmod 0755 /usr/local/bin/virt-install

EXPOSE 8760

CMD ["python", "-m", "uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8760", "--no-access-log"]
