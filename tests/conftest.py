"""Make each independently runnable service importable during repository tests."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIRS = (
    PROJECT_DIR / "vm-manager",
    PROJECT_DIR / "diagnostic-gateway",
    PROJECT_DIR / "agent-service",
    PROJECT_DIR / "docs-service",
    PROJECT_DIR / "progress-worker",
)

for service_dir in reversed(SERVICE_DIRS):
    sys.path.insert(0, str(service_dir))
