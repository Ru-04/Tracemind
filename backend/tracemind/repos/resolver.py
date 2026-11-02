"""Release-to-commit resolution with explicit confidence (M2-7).

`verified` is returned for exactly one situation: the application's own telemetry or deployment
metadata supplied a **full 40-character commit id** and that commit exists in the registered
repository. Everything else is `unknown`, with a stable reason. There is no `estimated` outcome
in M2, and nothing is inferred from release names, tags, branch names, timestamps, or proximity.

"Verified" means the commit exists in this repository and was asserted by the source; it does
not prove what is deployed beyond that assertion, and the response says where the claim came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import GitAdapter

_FULL = re.compile(r"^[0-9a-fA-F]{40}$")
_ABBREV = re.compile(r"^[0-9a-fA-F]{7,39}$")


@dataclass(frozen=True)
class CommitResolution:
    commit_sha: str | None
    confidence: str  # "verified" | "unknown"
    source: str | None
    reason: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
        }


def _unknown(reason: str) -> CommitResolution:
    return CommitResolution(None, "unknown", None, reason)


def resolve_release_commit(
    git: GitAdapter | None, *, release: str | None = None, commit_sha: object | None = None
) -> CommitResolution:
    """`release` is accepted so callers can pass what telemetry carried, but it is deliberately
    never used to derive a commit."""
    del release
    if git is None:
        return _unknown("no_repository")
    if commit_sha is None or commit_sha == "":
        return _unknown("no_commit_in_telemetry")
    if not isinstance(commit_sha, str):
        return _unknown("invalid_commit_sha")
    if _ABBREV.match(commit_sha):
        return _unknown("abbreviated_sha")  # not exact: a prefix could name several commits
    if not _FULL.match(commit_sha):
        return _unknown("invalid_commit_sha")  # branch names, tags, expressions: never resolved
    sha = commit_sha.lower()
    try:
        exists = git.commit_exists(sha)
    except RepoError:
        return _unknown("repository_unavailable")
    if not exists:
        return _unknown("commit_not_in_repository")
    return CommitResolution(sha, "verified", "telemetry.commit_sha", None)
