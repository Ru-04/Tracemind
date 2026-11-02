"""Repository registration. Local repositories only, under explicitly configured roots."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracemind.config import Settings
from tracemind.observability.logging import LOGGER_NAME, log
from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import GitAdapter
from tracemind.repos.store import AlreadyRegisteredError, RepositoryStore, repository_path
from tracemind.repos.validate import resolve_repository_path

_logger = logging.getLogger(LOGGER_NAME + ".repos")
MAX_DISPLAY_NAME = 128


class RepositoryService:
    def __init__(
        self,
        store: RepositoryStore,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._settings = settings
        self._clock = clock

    @property
    def store(self) -> RepositoryStore:
        return self._store

    def _forbidden(self) -> list[Path]:
        return [self._settings.data_dir]

    def open_git(self, project_id: str, repository_id: str) -> GitAdapter | None:
        """A GitAdapter for a registered repository, after re-validating its stored path.

        The path is checked again on every use, so a directory later replaced by a symlink to
        somewhere else is refused instead of followed."""
        stored = repository_path(self._store.engine, project_id, repository_id)
        if stored is None:
            return None
        resolved = resolve_repository_path(
            stored, self._settings.repo_roots, forbidden=self._forbidden()
        )
        git = GitAdapter(resolved)
        git.verify_repository()
        return git

    def register(self, project_id: str, display_name: object, path: object) -> dict[str, Any]:
        if (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name) > MAX_DISPLAY_NAME
            or any(ord(c) < 32 for c in display_name)
        ):
            raise RepoError("invalid_display_name")
        resolved = resolve_repository_path(
            path, self._settings.repo_roots, forbidden=self._forbidden()
        )
        git = GitAdapter(resolved)
        git.verify_repository()
        identity = git.identity()  # raises empty_repository for a repository with no commits
        try:
            record = self._store.create_repository(
                project_id=project_id,
                display_name=display_name.strip(),
                path=str(resolved),
                identity=identity,
                default_branch=git.default_branch(),
                now=self._clock(),
            )
        except AlreadyRegisteredError:
            raise RepoError("already_registered") from None
        log(
            _logger,
            logging.INFO,
            "repository registered",
            project_id=project_id,
            repository_id=record["repository_id"],
        )
        return record
