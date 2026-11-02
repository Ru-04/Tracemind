"""Shared fixtures. No credential is hardcoded: keys and DB passwords come from the environment
or are generated per test run."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tracemind.config import Settings
from tracemind.ingestion.redact import Redactor
from tracemind.projects import Project

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Read .env (gitignored) into the environment for local runs; CI sets real variables."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.split("#")[0].strip())


_load_dotenv()

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


@pytest.fixture
def project() -> Project:
    return Project("project-demo", frozenset({"payments-api", "demo-payments"}))


@pytest.fixture
def raw_event() -> dict[str, Any]:
    return {
        "project_id": "project-demo",
        "service": "payments-api",
        "environment": "development",
        "timestamp": "2026-10-28T10:15:30Z",
        "severity": "ERROR",
        "message": "Database connection failed",
        "error_type": "ConnectionError",
        "attributes": {"endpoint": "/payments", "retry_count": 3},
    }


@pytest.fixture
def make_key() -> Iterator[str]:
    yield secrets.token_urlsafe(32)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TRACEMIND_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("TRACEMIND_REQUIRE_DB") == "1":
            pytest.fail("TRACEMIND_TEST_DATABASE_URL is required (TRACEMIND_REQUIRE_DB=1)")
        pytest.skip("TRACEMIND_TEST_DATABASE_URL not set; start infra/docker-compose.yml")
    return url


@pytest.fixture(scope="session")
def graphify_bin() -> Path:
    """The pinned Graphify CLI (tools/install_graphify.py). Skipped locally if absent; required in CI."""
    configured = os.environ.get("TRACEMIND_GRAPHIFY_BIN")
    candidates = [Path(configured)] if configured else []
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "graphify.exe" if os.name == "nt" else "graphify"
    candidates.append(ROOT / ".graphify-venv" / scripts / exe)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if os.environ.get("TRACEMIND_REQUIRE_GRAPHIFY") == "1":
        pytest.fail("pinned Graphify not installed (python -m tools.install_graphify)")
    pytest.skip("pinned Graphify not installed (python -m tools.install_graphify)")
