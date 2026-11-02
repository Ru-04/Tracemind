"""Typed settings read from the environment. Secrets are never defaulted or echoed."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Invalid or missing configuration. Messages name settings, never their values."""


@dataclass(frozen=True)
class Settings:
    database_url: str | None = None
    projects_file: Path = Path("config/projects.json")
    max_body_bytes: int = 1_048_576
    max_events: int = 100
    max_attributes: int = 64
    max_attribute_depth: int = 6
    max_attributes_bytes: int = 16_384
    future_skew_seconds: int = 600
    min_incident_severity: str = "ERROR"
    redact_emails: bool = True
    rate_limit_burst: int = 1000
    rate_limit_per_second: float = 100.0
    repo_roots: tuple[Path, ...] = ()  # local repositories may only be registered under these
    data_dir: Path = Path("data")  # TraceMind-owned snapshots and graphs; never inside a repo root
    graphify_bin: Path | None = None  # the pinned, isolated Graphify CLI (ADR-0003)
    scan_timeout_seconds: float = 300.0
    scan_max_attempts: int = 3
    scan_lease_seconds: float = 900.0
    scan_backoff_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.min_incident_severity not in ("ERROR", "CRITICAL"):
            raise ConfigError("min_incident_severity must be ERROR or CRITICAL")
        if self.max_events < 1 or self.max_body_bytes < 1:
            raise ConfigError("max_events and max_body_bytes must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        d = cls()
        try:
            return cls(
                database_url=e.get("TRACEMIND_DATABASE_URL") or None,
                projects_file=Path(e.get("TRACEMIND_PROJECTS_FILE", str(d.projects_file))),
                max_body_bytes=int(e.get("TRACEMIND_MAX_BODY_BYTES", d.max_body_bytes)),
                max_events=int(e.get("TRACEMIND_MAX_EVENTS", d.max_events)),
                min_incident_severity=e.get(
                    "TRACEMIND_MIN_INCIDENT_SEVERITY", d.min_incident_severity
                ),
                redact_emails=e.get("TRACEMIND_REDACT_EMAILS", "1") not in ("0", "false", "False"),
                rate_limit_burst=int(e.get("TRACEMIND_RATE_LIMIT_BURST", d.rate_limit_burst)),
                rate_limit_per_second=float(
                    e.get("TRACEMIND_RATE_LIMIT_PER_SECOND", d.rate_limit_per_second)
                ),
                repo_roots=tuple(
                    Path(part)
                    for part in e.get("TRACEMIND_REPO_ROOTS", "").split(os.pathsep)
                    if part
                ),
                data_dir=Path(e.get("TRACEMIND_DATA_DIR", str(d.data_dir))),
                graphify_bin=Path(e["TRACEMIND_GRAPHIFY_BIN"])
                if e.get("TRACEMIND_GRAPHIFY_BIN")
                else None,
                scan_timeout_seconds=float(
                    e.get("TRACEMIND_SCAN_TIMEOUT_SECONDS", d.scan_timeout_seconds)
                ),
                scan_max_attempts=int(e.get("TRACEMIND_SCAN_MAX_ATTEMPTS", d.scan_max_attempts)),
                scan_lease_seconds=float(
                    e.get("TRACEMIND_SCAN_LEASE_SECONDS", d.scan_lease_seconds)
                ),
                scan_backoff_seconds=float(
                    e.get("TRACEMIND_SCAN_BACKOFF_SECONDS", d.scan_backoff_seconds)
                ),
            )
        except ValueError as exc:
            raise ConfigError("a numeric TRACEMIND_* setting is not a valid number") from exc
