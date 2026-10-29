from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import os
import re
import signal
import time
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Annotated

from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field


PROJECT_DIR = Path(__file__).resolve().parent
LIBVIRT_DIR = Path(
    os.getenv("AIVIRTEACH_LIBVIRT_DIR", str(PROJECT_DIR / "libvirt"))
).resolve()
VM_CONTROL_SCRIPT = LIBVIRT_DIR / "scripts" / "vm-control.sh"
CREATE_VM_SCRIPT = LIBVIRT_DIR / "scripts" / "create-learner-vm.sh"
LAB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
VM_INSTANCE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SENSITIVE_SUBPROCESS_ENV = frozenset(
    {
        "AIVIRTEACH_API_TOKEN",
        "AIVIRTEACH_SESSION_TOKEN",
        "AIVIRTEACH_GUACAMOLE_JSON_SECRET",
    }
)

COMMAND_TIMEOUT_SECONDS = int(os.getenv("AIVIRTEACH_COMMAND_TIMEOUT", "30"))
CREATE_TIMEOUT_SECONDS = int(os.getenv("AIVIRTEACH_CREATE_TIMEOUT", "180"))


def _cors_origins() -> list[str]:
    raw = os.getenv(
        "AIVIRTEACH_VM_CORS_ORIGINS",
        "http://127.0.0.1:8780,http://localhost:8780",
    )
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]

app = FastAPI(
    title="AIVirTeach VM Manager",
    version="1.0.0",
    description="A restricted HTTP interface for the AIVirTeach libvirt scripts.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

_lab_locks: dict[str, asyncio.Lock] = {}

admin_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AdminBearer",
    description="AIVIRTEACH_API_TOKEN — VM lifecycle and credentials.",
)

session_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="SessionBearer",
    description="AIVIRTEACH_SESSION_TOKEN — mint short-lived browser RDP sessions.",
)


class VMAction(str, Enum):
    START = "start"
    STOP = "stop"
    FORCE_STOP = "force-stop"
    REBOOT = "reboot"


class CreateVMRequest(BaseModel):
    lab_id: str = Field(min_length=1, max_length=63)
    memory_mb: int = Field(default=4096, ge=1024, le=32768)
    vcpus: int = Field(default=2, ge=1, le=16)
    autostart: bool = False


class OperationResponse(BaseModel):
    lab_id: str
    operation: str
    message: str


class VMListItem(BaseModel):
    lab_id: str = Field(description="libvirt domain name used by Labs APIs")
    vm_instance_id: str = Field(description="Immutable libvirt domain UUID")
    state: str = Field(description="Current libvirt domain state")


class VMListResponse(BaseModel):
    count: int = Field(ge=0)
    vms: list[VMListItem]


class BrowserSessionRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._@-]+$")


class BrowserSessionResponse(BaseModel):
    lab_id: str
    state: str
    data: str | None = None
    expires_at: int | None = None


def _validate_lab_id(lab_id: str) -> str:
    if not LAB_ID_RE.fullmatch(lab_id):
        raise HTTPException(
            status_code=422,
            detail="lab_id must contain 1-63 letters, numbers, '.', '_' or '-'.",
        )
    return lab_id


def _lab_lock(lab_id: str) -> asyncio.Lock:
    lock = _lab_locks.get(lab_id)
    if lock is None:
        lock = asyncio.Lock()
        _lab_locks[lab_id] = lock
    return lock


def _api_token() -> str:
    return os.getenv("AIVIRTEACH_API_TOKEN", "")


def _session_token() -> str:
    return os.getenv("AIVIRTEACH_SESSION_TOKEN", "")


def _verify_script_layout() -> None:
    missing = [
        str(path)
        for path in (VM_CONTROL_SCRIPT, CREATE_VM_SCRIPT)
        if not path.is_file()
    ]
    if missing:
        raise RuntimeError(f"Required libvirt scripts not found: {', '.join(missing)}")


async def require_api_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(admin_bearer)],
) -> None:
    expected = _api_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_API_TOKEN is not configured.",
        )

    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_session_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(session_bearer)
    ],
) -> None:
    expected = _session_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_SESSION_TOKEN is not configured.",
        )

    admin_token = _api_token()
    if admin_token and hmac.compare_digest(expected, admin_token):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_SESSION_TOKEN must differ from AIVIRTEACH_API_TOKEN.",
        )

    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _error_status(output: str) -> int:
    lowered = output.lower()
    if "vm not found" in lowered or "domain not found" in lowered:
        return status.HTTP_404_NOT_FOUND
    # vm-control.sh 的 domain_exists 在 libvirt 本身出错时报这个；必须和 404 区分开，
    # 否则调用方会把"查不到"当成"VM 不存在"。
    if "libvirt unavailable" in lowered:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if "already exists" in lowered:
        return status.HTTP_409_CONFLICT
    if "timed out" in lowered:
        return status.HTTP_504_GATEWAY_TIMEOUT
    return status.HTTP_400_BAD_REQUEST


async def run_script(
    argv: Sequence[str | Path],
    *,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> str:
    env = os.environ.copy()
    for name in SENSITIVE_SUBPROCESS_ENV:
        env.pop(name, None)
    env["AIVIRTEACH_NONINTERACTIVE"] = "true"

    process = await asyncio.create_subprocess_exec(
        *(str(item) for item in argv),
        cwd=LIBVIRT_DIR,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Command timed out after {timeout_seconds} seconds.",
        ) from None

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = stderr_text or stdout_text or f"Command exited with {process.returncode}."
        raise HTTPException(status_code=_error_status(detail), detail=detail)

    return stdout_text


def _parse_key_value_lines(output: str, separator: str = "=") -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, found, value = line.partition(separator)
        if found:
            result[key.strip()] = value.strip()
    return result


def _parse_dominfo(output: str) -> dict[str, str]:
    return _parse_key_value_lines(output, separator=":")


def _parse_vm_list(output: str) -> list[VMListItem]:
    vms: list[VMListItem] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="VM inventory returned an invalid row.",
            )
        lab_id, vm_instance_id, vm_state = (field.strip() for field in fields)
        if (
            not lab_id
            or not VM_INSTANCE_ID_RE.fullmatch(vm_instance_id)
            or not vm_state
        ):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="VM inventory returned invalid identity data.",
            )
        vms.append(
            VMListItem(
                lab_id=lab_id,
                vm_instance_id=vm_instance_id,
                state=vm_state,
            )
        )
    return vms


async def _rdp_ready(ip_address: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip_address, port), timeout=1.5
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


def _rdp_allowed_networks() -> list[
    ipaddress.IPv4Network | ipaddress.IPv6Network
]:
    try:
        networks = [
            ipaddress.ip_network(item.strip())
            for item in os.getenv(
                "AIVIRTEACH_RDP_ALLOWED_CIDRS", "192.168.122.0/24"
            ).split(",")
            if item.strip()
        ]
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_RDP_ALLOWED_CIDRS is invalid.",
        ) from error
    if not networks:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_RDP_ALLOWED_CIDRS is invalid.",
        )
    return networks


def _validate_rdp_ip(
    value: str,
    allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The VM manager returned an invalid RDP network address.",
        ) from error

    if not any(address in network for network in allowed_networks):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The VM RDP address is outside the configured lab networks.",
        )
    return str(address)


def _guacamole_secret() -> bytes:
    value = os.getenv("AIVIRTEACH_GUACAMOLE_JSON_SECRET", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_GUACAMOLE_JSON_SECRET is not configured.",
        )
    return bytes.fromhex(value)


def _browser_session_ttl() -> int:
    try:
        configured = int(os.getenv("AIVIRTEACH_BROWSER_SESSION_TTL", "60"))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AIVIRTEACH_BROWSER_SESSION_TTL must be an integer.",
        ) from error
    return min(120, max(15, configured))


def _parse_rdp_credentials(output: str) -> tuple[str, str, int]:
    credentials = _parse_key_value_lines(output)
    username = credentials.get("username", "learner")
    password = credentials.get("password", "")
    try:
        rdp_port = int(credentials.get("rdp_port", "3389"))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RDP credentials are unavailable.",
        ) from error

    if not username or not password or rdp_port != 3389:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RDP credentials are unavailable.",
        )
    return username, password, rdp_port


def _encrypt_guacamole_payload(payload: dict[str, object]) -> str:
    key = _guacamole_secret()
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signer = crypto_hmac.HMAC(key, hashes.SHA256())
    signer.update(plaintext)
    signed = signer.finalize() + plaintext

    padder = padding.PKCS7(128).padder()
    padded = padder.update(signed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


@app.on_event("startup")
async def validate_startup() -> None:
    _verify_script_layout()


@app.get("/health", tags=["service"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/vms",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def create_vm(request: CreateVMRequest) -> dict[str, str | int | bool]:
    lab_id = _validate_lab_id(request.lab_id)
    argv: list[str | Path] = [
        CREATE_VM_SCRIPT,
        lab_id,
        "--memory",
        str(request.memory_mb),
        "--vcpus",
        str(request.vcpus),
    ]
    if request.autostart:
        argv.append("--autostart")

    async with _lab_lock(lab_id):
        output = await run_script(argv, timeout_seconds=CREATE_TIMEOUT_SECONDS)

    parsed = _parse_key_value_lines(output, separator=":")
    vm_instance_id = parsed.get("VM instance ID", "")
    if not VM_INSTANCE_ID_RE.fullmatch(vm_instance_id):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="VM creation did not return a valid instance ID.",
        )
    return {
        "lab_id": lab_id,
        "vm_instance_id": vm_instance_id,
        "username": parsed.get("Username", "learner"),
        "rdp_password": parsed.get("RDP password", ""),
        "rdp_port": 3389,
        "memory_mb": request.memory_mb,
        "vcpus": request.vcpus,
        "autostart": request.autostart,
    }


@app.get(
    "/v1/vms",
    response_model=VMListResponse,
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
    summary="List all virtual machine IDs",
)
async def list_vms() -> VMListResponse:
    """Return every libvirt domain name, immutable UUID, and current state."""

    vms = _parse_vm_list(await run_script([VM_CONTROL_SCRIPT, "list"]))
    return VMListResponse(count=len(vms), vms=vms)


@app.get(
    "/v1/vms/{lab_id}/status",
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def vm_status(lab_id: str) -> dict[str, str]:
    lab_id = _validate_lab_id(lab_id)
    output = await run_script([VM_CONTROL_SCRIPT, "status", lab_id])
    return _parse_dominfo(output)


@app.get(
    "/v1/vms/{lab_id}/ip",
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def vm_ip(lab_id: str) -> dict[str, str]:
    lab_id = _validate_lab_id(lab_id)
    ip_address = await run_script([VM_CONTROL_SCRIPT, "ip", lab_id])
    return {"lab_id": lab_id, "ip_address": ip_address}


@app.get(
    "/v1/vms/{lab_id}/vnc",
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def vm_vnc(lab_id: str) -> dict[str, str | int]:
    lab_id = _validate_lab_id(lab_id)
    output = await run_script([VM_CONTROL_SCRIPT, "vnc", lab_id])
    parsed = _parse_key_value_lines(output)
    response: dict[str, str | int] = {"lab_id": lab_id, **parsed}
    if "port" in response:
        response["port"] = int(response["port"])
    return response


@app.get(
    "/v1/vms/{lab_id}/credentials",
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def vm_credentials(lab_id: str) -> dict[str, str | int]:
    lab_id = _validate_lab_id(lab_id)
    output = await run_script([VM_CONTROL_SCRIPT, "credentials", lab_id])
    parsed: dict[str, str | int] = _parse_key_value_lines(output)
    if "rdp_port" in parsed:
        parsed["rdp_port"] = int(parsed["rdp_port"])
    return parsed


@app.post(
    "/v1/vms/{lab_id}/browser-sessions",
    response_model=BrowserSessionResponse,
    dependencies=[Depends(require_session_token)],
    tags=["workspace"],
)
async def create_browser_session(
    lab_id: str, request: BrowserSessionRequest, response: Response
) -> BrowserSessionResponse:
    """Start an assigned VM and mint a short-lived opaque Guacamole ticket."""

    lab_id = _validate_lab_id(lab_id)
    response.headers["Cache-Control"] = "no-store"
    ttl_seconds = _browser_session_ttl()
    _guacamole_secret()
    allowed_networks = _rdp_allowed_networks()

    async with _lab_lock(lab_id):
        output = await run_script([VM_CONTROL_SCRIPT, "status", lab_id])
        vm_state = _parse_dominfo(output).get("State", "unknown").strip().lower()

        if vm_state != "running":
            if vm_state in {"shut off", "shutoff"}:
                await run_script([VM_CONTROL_SCRIPT, "start", lab_id])
                return BrowserSessionResponse(lab_id=lab_id, state="starting")
            return BrowserSessionResponse(
                lab_id=lab_id, state=vm_state or "unavailable"
            )

        try:
            ip_address = await run_script([VM_CONTROL_SCRIPT, "ip", lab_id])
        except HTTPException as error:
            if "no ipv4 address" in str(error.detail).lower():
                return BrowserSessionResponse(lab_id=lab_id, state="starting")
            raise

        ip_address = _validate_rdp_ip(ip_address, allowed_networks)
        if not await _rdp_ready(ip_address, 3389):
            return BrowserSessionResponse(lab_id=lab_id, state="starting")

        username, password, rdp_port = _parse_rdp_credentials(
            await run_script([VM_CONTROL_SCRIPT, "credentials", lab_id])
        )
        expires_at = int(time.time() * 1000) + ttl_seconds * 1000
        data = _encrypt_guacamole_payload(
            {
                "username": request.subject,
                "expires": expires_at,
                "connections": {
                    lab_id: {
                        "protocol": "rdp",
                        "parameters": {
                            "hostname": ip_address,
                            "port": str(rdp_port),
                            "username": username,
                            "password": password,
                            "security": "any",
                            "ignore-cert": "true",
                            "resize-method": "display-update",
                            "enable-wallpaper": "true",
                        },
                    }
                },
            }
        )
        return BrowserSessionResponse(
            lab_id=lab_id, state="ready", data=data, expires_at=expires_at
        )


@app.post(
    "/v1/vms/{lab_id}/actions/{action}",
    response_model=OperationResponse,
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def vm_action(lab_id: str, action: VMAction) -> OperationResponse:
    lab_id = _validate_lab_id(lab_id)
    async with _lab_lock(lab_id):
        output = await run_script([VM_CONTROL_SCRIPT, action.value, lab_id])
    return OperationResponse(
        lab_id=lab_id,
        operation=action.value,
        message=output or "Command completed.",
    )


@app.delete(
    "/v1/vms/{lab_id}",
    response_model=OperationResponse,
    dependencies=[Depends(require_api_token)],
    tags=["vms"],
)
async def delete_vm(
    lab_id: str,
    confirm: Annotated[bool, Query(description="Must be true to delete the VM")] = False,
) -> OperationResponse:
    lab_id = _validate_lab_id(lab_id)
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Deletion requires confirm=true.",
        )

    async with _lab_lock(lab_id):
        output = await run_script([VM_CONTROL_SCRIPT, "delete", lab_id, "--yes"])
    return OperationResponse(
        lab_id=lab_id,
        operation="delete",
        message=output or "VM deleted.",
    )
