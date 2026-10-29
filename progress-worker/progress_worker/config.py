from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ConfigurationError(ValueError):
    pass


def _number(name: str, default: str, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _integer(name: str, default: str, minimum: int, maximum: int) -> int:
    value = _number(name, default, minimum, maximum)
    if not value.is_integer():
        raise ConfigurationError(f"{name} must be an integer")
    return int(value)


def _url(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip().rstrip("/")
    value = urlsplit(raw)
    if (
        value.scheme not in {"http", "https"}
        or not value.hostname
        or value.username is not None
        or value.password is not None
        or value.query
        or value.fragment
    ):
        raise ConfigurationError(f"{name} must be a plain HTTP(S) base URL")
    return raw


def _token(name: str) -> str:
    value = os.getenv(name, "").strip()
    if len(value) < 16 or len(value) > 512:
        raise ConfigurationError(f"{name} must contain a 16-512 character token")
    return value


@dataclass(frozen=True)
class Settings:
    worker_id: str
    server_url: str
    server_token: str
    diagnostic_url: str
    diagnostic_token: str
    database_path: Path
    poll_seconds: float
    heartbeat_seconds: int
    request_timeout_seconds: float
    batch_size: int
    max_probes_per_target: int
    unknown_backoff_max_seconds: int
    retry_base_seconds: float
    retry_max_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        worker_id = os.getenv("AIVIRTEACH_PROGRESS_WORKER_ID", "").strip()
        if not WORKER_ID_RE.fullmatch(worker_id):
            raise ConfigurationError(
                "AIVIRTEACH_PROGRESS_WORKER_ID must be a safe 1-128 character ID"
            )
        server_token = _token("AIVIRTEACH_PROGRESS_SERVER_TOKEN")
        diagnostic_token = _token("AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN")
        if server_token == diagnostic_token:
            raise ConfigurationError("Progress and diagnostic tokens must be different")

        raw_path = os.getenv(
            "AIVIRTEACH_PROGRESS_DB",
            "/var/lib/aivirteach-progress/progress.sqlite3",
        ).strip()
        if not raw_path or "\0" in raw_path:
            raise ConfigurationError("AIVIRTEACH_PROGRESS_DB is invalid")

        poll_seconds = _number("AIVIRTEACH_PROGRESS_POLL_SECONDS", "10", 1, 3_600)
        retry_base = _number("AIVIRTEACH_PROGRESS_RETRY_BASE_SECONDS", "2", 0.1, 600)
        retry_max = _number("AIVIRTEACH_PROGRESS_RETRY_MAX_SECONDS", "300", 1, 86_400)
        if retry_max < retry_base:
            raise ConfigurationError("retry max must not be smaller than retry base")

        return cls(
            worker_id=worker_id,
            server_url=_url(
                "AIVIRTEACH_PROGRESS_SERVER_URL",
                "http://127.0.0.1:4000/api/v1",
            ),
            server_token=server_token,
            diagnostic_url=_url(
                "AIVIRTEACH_DIAGNOSTIC_URL", "http://127.0.0.1:8765"
            ),
            diagnostic_token=diagnostic_token,
            database_path=Path(raw_path).expanduser().resolve(),
            poll_seconds=poll_seconds,
            heartbeat_seconds=_integer(
                "AIVIRTEACH_PROGRESS_HEARTBEAT_SECONDS", "300", 30, 86_400
            ),
            request_timeout_seconds=_number(
                "AIVIRTEACH_PROGRESS_REQUEST_TIMEOUT", "50", 5, 120
            ),
            batch_size=_integer("AIVIRTEACH_PROGRESS_BATCH_SIZE", "50", 1, 100),
            max_probes_per_target=_integer(
                "AIVIRTEACH_PROGRESS_MAX_PROBES_PER_TARGET", "3", 1, 7
            ),
            unknown_backoff_max_seconds=_integer(
                "AIVIRTEACH_PROGRESS_UNKNOWN_BACKOFF_MAX_SECONDS", "300", 10, 3_600
            ),
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
        )
