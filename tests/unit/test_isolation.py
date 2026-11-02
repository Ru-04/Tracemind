"""M1-9: deterministic ingestion and grouping have no LLM / external-provider dependency, and
the pipeline runs with all outbound network access blocked."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest
from tracemind.ingestion.adapters import AdapterItem
from tracemind.ingestion.models import validate_ingest
from tracemind.ingestion.normalize import prepare_event

from tests.conftest import NOW, ROOT

BACKEND = ROOT / "backend" / "tracemind"
DETERMINISTIC = [BACKEND / "ingestion", BACKEND / "fingerprint.py", BACKEND / "projects"]
FORBIDDEN = {
    "groq",
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "urllib",
    "urllib3",
    "aiohttp",
    "socket",
    "snowflake",
    "langchain",
    "litellm",
    "subprocess",
}


def imported_modules(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


def files() -> list[Path]:
    out: list[Path] = []
    for target in DETERMINISTIC:
        out.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return out


@pytest.mark.parametrize("path", files(), ids=lambda p: str(p.relative_to(BACKEND)))
def test_deterministic_modules_import_no_llm_or_network_library(path: Path) -> None:
    assert not imported_modules(path) & FORBIDDEN


def test_no_llm_provider_is_a_runtime_dependency_yet() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    runtime = text.split("[project.optional-dependencies]")[0]
    assert "groq" not in runtime.lower() and "snowflake" not in runtime.lower()


def test_prepare_event_runs_with_network_blocked(
    monkeypatch, project, settings, redactor, raw_event
) -> None:
    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    prepared = prepare_event(
        validate_ingest(raw_event), project, now=NOW, settings=settings, redactor=redactor
    )
    assert prepared.incident_eligible and len(prepared.event.fingerprint) == 64
    assert AdapterItem(event=raw_event).event is raw_event
