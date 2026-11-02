from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tracemind.config import ConfigError

_MIN_KEY_LENGTH = 24
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class Project:
    project_id: str
    allowed_services: frozenset[str]
    # Digest only: the plaintext key is never held after loading. Excluded from repr.
    key_digest: bytes = field(repr=False, compare=False, default=b"")


class ProjectRegistry(Protocol):
    def authenticate(self, presented_key: str) -> Project | None: ...


class ConfigProjectRegistry:
    def __init__(self, projects: list[Project]) -> None:
        self._projects = projects

    @classmethod
    def from_file(cls, path: Path, env: Mapping[str, str]) -> ConfigProjectRegistry:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data["projects"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"projects file {path} is missing or invalid") from exc
        projects: list[Project] = []
        seen_ids: set[str] = set()
        seen_digests: set[bytes] = set()
        for entry in entries:
            try:
                project_id = entry["project_id"]
                services = frozenset(entry["allowed_services"])
                key_env = entry["ingest_key_env"]
            except (KeyError, TypeError) as exc:
                raise ConfigError(
                    "each project needs project_id, allowed_services, ingest_key_env"
                ) from exc
            if not isinstance(project_id, str) or not _ID.match(project_id):
                raise ConfigError("project_id must match ^[a-z0-9][a-z0-9._-]{0,127}$")
            if project_id in seen_ids:
                raise ConfigError(f"duplicate project_id {project_id}")
            key = env.get(key_env, "")
            if len(key) < _MIN_KEY_LENGTH:
                raise ConfigError(
                    f"environment variable {key_env} must hold an ingest key of at least "
                    f"{_MIN_KEY_LENGTH} characters"
                )
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            if digest in seen_digests:
                raise ConfigError("two projects share the same ingest key")
            seen_ids.add(project_id)
            seen_digests.add(digest)
            projects.append(Project(project_id, services, digest))
        if not projects:
            raise ConfigError("no projects configured")
        return cls(projects)

    def authenticate(self, presented_key: str) -> Project | None:
        digest = hashlib.sha256(presented_key.encode("utf-8", "replace")).digest()
        match: Project | None = None
        for project in self._projects:  # no early exit: constant work per project
            if hmac.compare_digest(project.key_digest, digest):
                match = project
        return match
