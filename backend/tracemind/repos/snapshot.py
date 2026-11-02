"""Snapshots: a TraceMind-owned copy of the files of one exact commit.

Graphify writes into the directory it scans, so the monitored repository is never scanned;
this module builds the copy it scans instead. Files are read from Git objects
(`ls-tree` + `cat-file --batch`), never from the working tree, so the snapshot is exactly the
commit. It is built in a temporary directory and renamed into place, so a failure never leaves
a partial snapshot visible.

Only regular files are written. Symlinks, submodules, unsafe or non-UTF-8 paths, case-colliding
names and oversized files are skipped and *counted* in the report, so coverage claims stay
honest. Exceeding a global limit fails the export instead.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import FileChange, GitAdapter, TreeEntry
from tracemind.repos.validate import is_safe_snapshot_path

# Graphify's own output directory lives inside the snapshot; it is not part of the commit.
RESERVED_TOP_LEVEL = "graphify-out"


@dataclass(frozen=True)
class SnapshotLimits:
    max_files: int = 20_000
    max_total_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024


@dataclass
class SnapshotReport:
    commit_sha: str
    files_written: int = 0
    bytes_written: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


@dataclass
class VerifyReport:
    ok: bool
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)


def classify_entry(entry: TreeEntry, limits: SnapshotLimits, seen_lower: set[str]) -> str | None:
    """Reason this entry is not materialized, or None if it is. Mutates `seen_lower`."""
    if entry.mode == "120000":
        return "symlink"
    if entry.mode == "160000" or entry.type == "commit":
        return "submodule"
    if entry.type != "blob" or entry.mode not in ("100644", "100755"):
        return "unsupported_mode"
    try:
        entry.path.encode("utf-8")
    except UnicodeEncodeError:
        return "non_utf8_path"
    if not is_safe_snapshot_path(entry.path) or entry.path.split("/")[0] == RESERVED_TOP_LEVEL:
        return "unsafe_path"
    if entry.size > limits.max_file_bytes:
        return "too_large"
    lowered = entry.path.lower()
    if lowered in seen_lower:
        return "case_collision"
    seen_lower.add(lowered)
    return None


def plan_export(
    entries: Sequence[TreeEntry], limits: SnapshotLimits
) -> tuple[list[TreeEntry], Counter[str]]:
    included: list[TreeEntry] = []
    skipped: Counter[str] = Counter()
    seen: set[str] = set()
    total = 0
    for entry in sorted(entries, key=lambda e: e.path):
        reason = classify_entry(entry, limits, seen)
        if reason:
            skipped[reason] += 1
            continue
        included.append(entry)
        total += entry.size
        if len(included) > limits.max_files:
            raise RepoError("snapshot_limit_exceeded", "too_many_files")
        if total > limits.max_total_bytes:
            raise RepoError("snapshot_limit_exceeded", "total_size")
    return included, skipped


def _safe_target(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        raise RepoError("snapshot_inconsistent", "path_escape")
    return target


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git object id


def export_snapshot(
    git: GitAdapter, revision: object, dest: Path, limits: SnapshotLimits | None = None
) -> SnapshotReport:
    """Materialize the commit's files into `dest` (which must not exist), atomically."""
    limits = limits or SnapshotLimits()
    sha = git.resolve_commit(revision)
    included, skipped = plan_export(git.ls_tree(sha), limits)
    if dest.exists():
        raise RepoError("snapshot_inconsistent", "destination_exists")
    tmp = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    report = SnapshotReport(sha, skipped=dict(skipped))
    try:
        with git.blob_reader() as reader:
            for entry in included:
                data = reader.read(entry.oid, max_bytes=limits.max_file_bytes)
                if len(data) != entry.size:
                    raise RepoError("snapshot_inconsistent", "size_mismatch")
                target = _safe_target(tmp, entry.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                report.files_written += 1
                report.bytes_written += len(data)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return report


def apply_changes(
    git: GitAdapter,
    snapshot_dir: Path,
    changes: Sequence[FileChange],
    limits: SnapshotLimits | None = None,
) -> SnapshotReport:
    """Move an existing snapshot forward by applying a Git diff (add / modify / delete / rename).

    Mutates `snapshot_dir` in place. Callers needing atomicity operate on a copy and swap it in
    (see ScanService)."""
    limits = limits or SnapshotLimits()
    report = SnapshotReport(commit_sha="")
    seen: set[str] = set()
    with git.blob_reader() as reader:
        for change in changes:
            if change.status in ("D", "R"):
                old = change.old_path if change.status == "R" else change.path
                if old and is_safe_snapshot_path(old):
                    _remove(snapshot_dir, old)
            if change.status == "D":
                continue
            assert change.oid is not None and change.mode is not None
            entry = TreeEntry(
                change.mode,
                "blob" if change.mode in ("100644", "100755") else "other",
                change.oid,
                0,
                change.path,
            )
            reason = classify_entry(entry, limits, seen)
            if reason:
                if is_safe_snapshot_path(change.path):
                    _remove(snapshot_dir, change.path)
                report.skipped[reason] = report.skipped.get(reason, 0) + 1
                continue
            try:
                data = reader.read(change.oid, max_bytes=limits.max_file_bytes)
            except RepoError as exc:
                if exc.detail != "file_too_large":
                    raise
                _remove(snapshot_dir, change.path)
                report.skipped["too_large"] = report.skipped.get("too_large", 0) + 1
                continue
            target = _safe_target(snapshot_dir, change.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            report.files_written += 1
            report.bytes_written += len(data)
    return report


def _remove(root: Path, rel: str) -> None:
    target = _safe_target(root, rel)
    if target.is_file():
        target.unlink()
        parent = target.parent
        root_resolved = root.resolve()
        while parent != root_resolved and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def verify_snapshot(
    git: GitAdapter, snapshot_dir: Path, revision: object, limits: SnapshotLimits | None = None
) -> VerifyReport:
    """Compare a snapshot with the commit it claims to be: same files, same content hashes."""
    limits = limits or SnapshotLimits()
    included, _ = plan_export(git.ls_tree(revision), limits)
    expected = {e.path: e.oid for e in included}
    actual: dict[str, Path] = {}
    root = snapshot_dir.resolve()
    for current, dirs, files in os.walk(root):
        if Path(current) == root:
            dirs[:] = [d for d in dirs if d != RESERVED_TOP_LEVEL]
        for name in files:
            path = Path(current) / name
            actual[path.relative_to(root).as_posix()] = path
    report = VerifyReport(ok=True)
    report.missing = sorted(set(expected) - set(actual))
    report.extra = sorted(set(actual) - set(expected))
    for rel in sorted(set(expected) & set(actual)):
        if _git_blob_sha(actual[rel].read_bytes()) != expected[rel]:
            report.mismatched.append(rel)
    report.ok = not (report.missing or report.extra or report.mismatched)
    return report
