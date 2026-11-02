"""Deployment records (M3 plan 4.3, check M3-11).

A deployment says "release R reached environment E at time T", optionally with the commit the
caller says it was built from. The commit link goes through the M2 resolver: it is `verified`
ONLY for a full 40-character SHA that exists in a registered repository of the same project,
and `unknown` otherwise, with a stable reason. Nothing is inferred from timestamps, release
names, tags or branches, and the link is resolved once when the record is written (the record
is immutable). A commit that was not yet in the repository at that moment stays `unknown`; the
fix is a new record, never an edit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from tracemind.config import Settings
from tracemind.deployments.store import DeploymentRow, DeploymentStore, InsertResult
from tracemind.errors import Rejected
from tracemind.ingestion.normalize import parse_timestamp
from tracemind.observability.logging import LOGGER_NAME, log
from tracemind.projects import Project
from tracemind.repos.errors import RepoError
from tracemind.repos.resolver import CommitResolution, resolve_release_commit
from tracemind.repos.service import RepositoryService

_logger = logging.getLogger(LOGGER_NAME + ".deployments")

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_TEXT = 128
SOURCE = "api"


class DeploymentError(Exception):
    """A rejected request. `code` is stable; `field` names the field, never its value."""

    def __init__(self, code: str, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


def _text(value: object, field: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise DeploymentError("missing_field", field)
        return None
    if not isinstance(value, str):
        raise DeploymentError(f"invalid_{field}", field)
    stripped = value.strip()
    if not stripped:
        if required:
            raise DeploymentError("missing_field", field)
        return None
    if len(stripped) > MAX_TEXT or _CONTROL.search(stripped):
        raise DeploymentError(f"invalid_{field}", field)
    return stripped


def _name(value: object, field: str, *, required: bool) -> str | None:
    text = _text(value, field, required=required)
    if text is None:
        return None
    lowered = text.lower()
    if not _NAME.match(lowered):
        raise DeploymentError(f"invalid_{field}", field)
    return lowered


class DeploymentService:
    def __init__(
        self,
        store: DeploymentStore,
        repositories: RepositoryService | None,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._repos = repositories
        self._settings = settings
        self._clock = clock

    @property
    def store(self) -> DeploymentStore:
        return self._store

    def record(
        self,
        project: Project,
        *,
        environment: object,
        release: object,
        deployed_at: object,
        service: object = None,
        commit_sha: object = None,
        repository_id: object = None,
        deployment_id: object = None,
    ) -> InsertResult:
        now = self._clock()
        env = _name(environment, "environment", required=True)
        svc = _name(service, "service", required=False)
        rel = _text(release, "release", required=True)
        assert env is not None and rel is not None
        if svc is not None and svc not in project.allowed_services:
            raise DeploymentError("unknown_service", "service")
        if not isinstance(deployed_at, str):
            raise DeploymentError("missing_field" if deployed_at is None else "invalid_deployed_at")
        try:
            when = parse_timestamp(
                deployed_at,
                now=now,
                future_skew=timedelta(seconds=self._settings.future_skew_seconds),
            )
        except Rejected as exc:
            code = exc.reason.replace("timestamp", "deployed_at")
            raise DeploymentError(code, "deployed_at") from None
        reported = _text(commit_sha, "commit_sha", required=False)
        repo_id = _text(repository_id, "repository_id", required=False)
        explicit_id = _text(deployment_id, "deployment_id", required=False)
        if explicit_id is not None and not _ID.match(explicit_id):
            raise DeploymentError("invalid_deployment_id", "deployment_id")

        resolution = self._resolve(project.project_id, repo_id, reported, rel)
        canonical = json.dumps(
            [project.project_id, env, svc, rel, when.isoformat(), repo_id, reported],
            separators=(",", ":"),
        )
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        row = DeploymentRow(
            project_id=project.project_id,
            deployment_id=explicit_id or f"dep-{content_hash[:32]}",
            environment=env,
            service=svc,
            release=rel,
            deployed_at=when,
            repository_id=repo_id,
            commit_sha_reported=reported,
            commit_sha=resolution.commit_sha,
            commit_confidence=resolution.confidence,
            commit_source=resolution.source,
            commit_reason=resolution.reason,
            source=SOURCE,
            created_at=now,
            content_hash=content_hash,
        )
        result = self._store.insert(row)
        log(
            _logger,
            logging.INFO,
            "deployment recorded",
            project_id=project.project_id,
            deployment_id=result.record["deployment_id"],
            outcome=result.status,
            commit_confidence=resolution.confidence,
        )
        return result

    def _resolve(
        self, project_id: str, repository_id: str | None, reported: str | None, release: str
    ) -> CommitResolution:
        if reported is None:
            return CommitResolution(None, "unknown", None, "no_commit_in_telemetry")
        if repository_id is None or self._repos is None:
            return resolve_release_commit(None, release=release, commit_sha=reported)
        if self._repos.store.get_repository(project_id, repository_id) is None:
            # Another project's repository is indistinguishable from a missing one.
            raise DeploymentError("repository_not_found", "repository_id")
        try:
            git = self._repos.open_git(project_id, repository_id)
        except RepoError:
            return CommitResolution(None, "unknown", None, "repository_unavailable")
        return resolve_release_commit(git, release=release, commit_sha=reported)
