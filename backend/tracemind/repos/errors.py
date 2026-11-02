"""Stable error codes for repository operations. Messages never echo caller-supplied values."""

from __future__ import annotations


class RepoError(Exception):
    """`code` is a stable machine code; `detail` is an optional stable sub-reason."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if detail is None else f"{code}:{detail}")
        self.code = code
        self.detail = detail


class GitCommandFailed(RepoError):
    """git exited non-zero. `returncode` is kept for callers; stderr is never exposed."""

    def __init__(self, returncode: int) -> None:
        super().__init__("git_failed")
        self.returncode = returncode
