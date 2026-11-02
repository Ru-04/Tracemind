"""Input validation for revisions, paths and repository locations.

Every value that reaches `git` or the filesystem passes through here first. Rejections carry a
stable code and a stable sub-reason, never the offending value."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from tracemind.repos.errors import RepoError

_HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MAX_PATH_LENGTH = 1024
MAX_COMPONENT_LENGTH = 255


def validate_revision(value: object) -> str:
    """A hex commit id (7-40 chars) or a conservative ref name. Anything else is rejected before
    git is invoked: no leading '-', whitespace, '..', '@{', ':', '^', '~', '?', '*', '[' or '\\'."""
    if not isinstance(value, str) or not value:
        raise RepoError("invalid_revision", "empty_or_not_text")
    if len(value) > 255:
        raise RepoError("invalid_revision", "too_long")
    if _HEX.match(value):
        return value.lower()
    if not _REF.match(value):
        raise RepoError("invalid_revision", "forbidden_characters")
    if (
        ".." in value
        or "//" in value
        or "/." in value
        or value.endswith(("/", ".", ".lock"))
        or value.startswith(".")
    ):
        raise RepoError("invalid_revision", "forbidden_pattern")
    return value


def validate_full_sha(value: object) -> str:
    if not isinstance(value, str) or not _FULL_SHA.match(value):
        raise RepoError("invalid_revision", "not_a_full_sha")
    return value


def validate_repo_path(value: object) -> str:
    """A repository-relative path, normalized to forward slashes. Rejects traversal, absolute and
    drive paths, NUL/control characters, backslashes, ':' (drive letters, NTFS streams), a leading
    '-', and any `.git` component."""
    if not isinstance(value, str) or not value:
        raise RepoError("invalid_path", "empty_or_not_text")
    if len(value) > MAX_PATH_LENGTH:
        raise RepoError("invalid_path", "too_long")
    if _CONTROL.search(value):
        raise RepoError("invalid_path", "control_characters")
    if "\\" in value:
        raise RepoError("invalid_path", "backslash")
    if value.startswith("/") or _DRIVE.match(value):
        raise RepoError("invalid_path", "absolute")
    if ":" in value:
        raise RepoError("invalid_path", "colon")
    if value.startswith("-"):
        raise RepoError("invalid_path", "leading_dash")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise RepoError("invalid_path", "traversal_or_empty_component")
        if part.lower() == ".git":
            raise RepoError("invalid_path", "git_directory")
        if len(part) > MAX_COMPONENT_LENGTH:
            raise RepoError("invalid_path", "component_too_long")
    return "/".join(parts)


def is_safe_snapshot_path(value: str) -> bool:
    """Stricter check used when writing files to disk: also refuses Windows-hostile names
    (reserved device names, trailing dots or spaces) so extraction behaves the same everywhere."""
    try:
        validate_repo_path(value)
    except RepoError:
        return False
    for part in value.split("/"):
        stem = part.split(".")[0].lower()
        if stem in _RESERVED or part.endswith((".", " ")):
            return False
    return True


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_repository_path(
    raw: object,
    allowed_roots: Sequence[Path],
    *,
    forbidden: Sequence[Path] = (),
) -> Path:
    """Canonicalize a repository path and confirm it is a Git work tree inside an allowed root.

    Symlinks and junctions are resolved *before* the containment check, so a link that points
    outside every allowed root is rejected; so is a `.git` link that does so."""
    if not isinstance(raw, str) or not raw:
        raise RepoError("invalid_repository_path", "empty_or_not_text")
    if len(raw) > MAX_PATH_LENGTH or "\x00" in raw or _CONTROL.search(raw):
        raise RepoError("invalid_repository_path", "forbidden_characters")
    if not allowed_roots:
        raise RepoError("invalid_repository_path", "no_allowed_roots_configured")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RepoError("invalid_repository_path", "not_absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RepoError("invalid_repository_path", "not_found") from None
    if not resolved.is_dir():
        raise RepoError("invalid_repository_path", "not_a_directory")

    roots = []
    for root in allowed_roots:
        try:
            roots.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not any(_inside(resolved, root) for root in roots):
        raise RepoError("invalid_repository_path", "outside_allowed_roots")

    for banned in forbidden:
        try:
            banned_resolved = banned.resolve()
        except (OSError, RuntimeError):
            continue
        if _inside(resolved, banned_resolved) or _inside(banned_resolved, resolved):
            raise RepoError("invalid_repository_path", "forbidden_location")

    _check_git_entry(resolved, roots)
    return resolved


def _check_git_entry(repo: Path, roots: Sequence[Path]) -> None:
    git_entry = repo / ".git"
    if not git_entry.exists() and not git_entry.is_symlink():
        raise RepoError("invalid_repository_path", "not_a_git_repository")
    try:
        real = git_entry.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RepoError("invalid_repository_path", "broken_git_entry") from None
    if real.is_file():  # worktree/submodule pointer: "gitdir: <path>"
        try:
            text = real.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeError):
            raise RepoError("invalid_repository_path", "broken_git_entry") from None
        if not text.startswith("gitdir:"):
            raise RepoError("invalid_repository_path", "broken_git_entry")
        pointer = Path(text.split(":", 1)[1].strip())
        if not pointer.is_absolute():
            pointer = real.parent / pointer
        try:
            real = pointer.resolve(strict=True)
        except (OSError, RuntimeError):
            raise RepoError("invalid_repository_path", "broken_git_entry") from None
    if not any(_inside(real, root) for root in roots):
        raise RepoError("invalid_repository_path", "git_dir_outside_allowed_roots")
