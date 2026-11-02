"""The only module that runs `git`. Read-only by construction.

- argv lists only, never a shell; every revision and path is validated first (see validate.py);
- a minimal, fixed environment: no user/system git config, no pager, no prompts, no optional
  index locks, literal pathspecs, no replace-refs;
- only read commands are used (`rev-parse`, `log`, `diff`, `ls-tree`, `cat-file`,
  `hash-object` without `-w`, `rev-list`, `symbolic-ref` without arguments that write);
- `--no-ext-diff --no-textconv` on diffs so repository-configured external programs never run;
- time and output caps on every call;
- no author name or e-mail is ever requested (M2 decision): commit metadata is limited to ids,
  parents, dates and a redacted subject.

Commit messages, paths and diffs are untrusted data.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Literal

from tracemind.ingestion.redact import Redactor
from tracemind.repos.errors import GitCommandFailed, RepoError
from tracemind.repos.validate import validate_full_sha, validate_repo_path, validate_revision

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_OUTPUT = 16 * 1024 * 1024
MAX_SUBJECT = 200
_ENV_KEEP = (
    "PATH",
    "SYSTEMROOT",
    "SystemRoot",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
)
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_redactor = Redactor()

Status = Literal["A", "M", "D", "R", "T"]


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    parents: tuple[str, ...]
    authored_at: datetime
    committed_at: datetime
    subject: str  # redacted and truncated; untrusted text


@dataclass(frozen=True)
class FileChange:
    path: str
    status: Status
    old_path: str | None
    additions: int | None
    deletions: int | None
    binary: bool
    oid: str | None  # blob id at the target revision (None for deletions)
    mode: str | None


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    type: str
    oid: str
    size: int  # -1 when git reports "-" (e.g. submodules)
    path: str


@dataclass(frozen=True)
class FileDiff:
    path: str
    patch: str  # raw, untrusted; callers redact before exposing
    truncated: bool
    binary: bool


def _build_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_PAGER="cat",
        GIT_OPTIONAL_LOCKS="0",
        GIT_LITERAL_PATHSPECS="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        LC_ALL="C",
    )
    return env


class GitAdapter:
    def __init__(
        self,
        repo_path: Path,
        *,
        git_cmd: Sequence[str] = ("git",),
        timeout: float = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> None:
        self.path = repo_path
        self._git = tuple(git_cmd)
        self._timeout = timeout
        self._max_output = max_output
        self._env = _build_env()

    # ---- process plumbing ---------------------------------------------------------------

    def _base_cmd(self) -> list[str]:
        return [
            *self._git,
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            "-C",
            str(self.path),
        ]

    def _exec(
        self,
        args: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
        truncate: bool = False,
    ) -> tuple[bytes, bool]:
        """Run git with a deadline and an output cap. Returns (stdout, truncated)."""
        limit = self._max_output if max_bytes is None else max_bytes
        deadline = self._timeout if timeout is None else timeout
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, validated inputs, no shell
            [*self._base_cmd(), *args],
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
            creationflags=flags,
        )
        assert proc.stdout is not None and proc.stderr is not None
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(deadline, _kill)
        timer.start()
        drain = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
        drain.start()
        writer: threading.Thread | None = None
        if input_bytes is not None:
            assert proc.stdin is not None
            writer = threading.Thread(target=_feed, args=(proc.stdin, input_bytes), daemon=True)
            writer.start()

        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    chunks.append(chunk[: len(chunk) - (total - limit)])
                    truncated = True
                    proc.kill()
                    break
                chunks.append(chunk)
        finally:
            timer.cancel()
            proc.wait()
            proc.stdout.close()
            drain.join(timeout=2)
            if writer is not None:
                writer.join(timeout=2)
        if timed_out.is_set():
            raise RepoError("timeout")
        if truncated and not truncate:
            raise RepoError("output_too_large")
        if proc.returncode != 0 and not truncated:
            raise GitCommandFailed(proc.returncode)
        return b"".join(chunks), truncated

    def _run(self, args: Sequence[str], **kwargs: object) -> bytes:
        out, _ = self._exec(args, **kwargs)  # type: ignore[arg-type]
        return out

    @contextmanager
    def _batch(self, timeout: float) -> Iterator[_BatchReader]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(  # noqa: S603
            [*self._base_cmd(), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env,
            creationflags=flags,
        )
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            yield _BatchReader(proc, timed_out)
        finally:
            timer.cancel()
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            proc.kill()
            proc.wait()
            if proc.stdout:
                proc.stdout.close()

    # ---- repository facts ---------------------------------------------------------------

    def verify_repository(self) -> None:
        """Check the path is a repository with the SHA-1 object format (the only one supported)."""
        try:
            fmt = self._run(["rev-parse", "--show-object-format"]).decode().strip()
        except GitCommandFailed:
            raise RepoError("invalid_repository_path", "not_a_git_repository") from None
        if fmt != "sha1":
            raise RepoError("unsupported_object_format")

    def head_sha(self) -> str:
        try:
            out = self._run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
        except GitCommandFailed:
            raise RepoError("empty_repository") from None
        return validate_full_sha(out.decode().strip())

    def default_branch(self) -> str | None:
        try:
            name = self._run(["symbolic-ref", "--short", "-q", "HEAD"]).decode().strip()
        except GitCommandFailed:
            return None  # detached HEAD
        return name if _BRANCH.match(name) else None

    def identity(self) -> str:
        """Stable repository identity: the root commit id(s). Survives moves and renames."""
        head = self.head_sha()
        out = self._run(["rev-list", "--max-parents=0", head]).decode().split()
        roots = sorted({validate_full_sha(s) for s in out})
        if not roots:
            raise RepoError("empty_repository")
        return "root:" + ",".join(roots[:10])

    def resolve_commit(self, revision: object) -> str:
        """Validate, then resolve to a full commit id that exists in this repository."""
        rev = validate_revision(revision)
        try:
            out = self._run(
                ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"]
            )
        except GitCommandFailed as exc:
            if exc.returncode != 1:  # 1 = "no such revision"; anything else is a broken repository
                raise
            raise RepoError("not_found", "revision") from None
        return validate_full_sha(out.decode().strip())

    def commit_exists(self, sha: str) -> bool:
        """True if `sha` (a full 40-hex id) names a commit in this repository."""
        try:
            self.resolve_commit(validate_full_sha(sha))
        except RepoError as exc:
            if exc.code == "not_found":
                return False
            raise
        return True

    # ---- history ------------------------------------------------------------------------

    def log(self, revision: object = "HEAD", *, limit: int = 50) -> list[CommitInfo]:
        sha = self.resolve_commit(revision)
        limit = max(1, min(int(limit), 500))
        out = self._run(
            [
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                f"--max-count={limit}",
                "--format=%H%x1f%P%x1f%aI%x1f%cI%x1f%s",
                sha,
            ]
        )
        return [self._parse_commit(rec) for rec in out.split(b"\x00") if rec.strip()]

    def commit(self, revision: object) -> CommitInfo:
        sha = self.resolve_commit(revision)
        out = self._run(
            [
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                "--max-count=1",
                "--format=%H%x1f%P%x1f%aI%x1f%cI%x1f%s",
                sha,
            ]
        )
        return self._parse_commit(out.split(b"\x00")[0])

    def commits_between(
        self, base: object | None, target: object, *, limit: int = 100
    ) -> list[CommitInfo]:
        """Commits reachable from `target` but not from `base`, newest first (all history when
        `base` is None). Both ends are validated and resolved to full ids before git sees them."""
        target_sha = self.resolve_commit(target)
        spec = target_sha if base is None else f"{self.resolve_commit(base)}..{target_sha}"
        out = self._run(
            [
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                f"--max-count={max(1, min(int(limit), 500))}",
                "--format=%H%x1f%P%x1f%aI%x1f%cI%x1f%s",
                spec,
            ]
        )
        return [self._parse_commit(rec) for rec in out.split(b"\x00") if rec.strip()]

    @staticmethod
    def _parse_commit(record: bytes) -> CommitInfo:
        parts = record.decode("utf-8", errors="replace").lstrip("\n").split("\x1f")
        if len(parts) != 5:
            raise RepoError("git_failed", "unparseable_log")
        sha, parents, authored, committed, subject = parts
        redacted, _ = _redactor.text(subject.strip())
        return CommitInfo(
            sha=validate_full_sha(sha),
            parents=tuple(validate_full_sha(p) for p in parents.split()),
            authored_at=datetime.fromisoformat(authored),
            committed_at=datetime.fromisoformat(committed),
            subject=redacted[:MAX_SUBJECT],
        )

    # ---- diffs --------------------------------------------------------------------------

    def _empty_tree(self) -> str:
        return validate_full_sha(
            self._run(["hash-object", "-t", "tree", "--stdin"], input_bytes=b"").decode().strip()
        )

    def diff_files(self, base: object | None, target: object) -> list[FileChange]:
        """Files changed between two commits (`base=None` means "from the empty tree")."""
        if base is not None:
            validate_revision(base)  # validate everything before the first git call
        validate_revision(target)
        target_sha = self.resolve_commit(target)
        base_ref = self._empty_tree() if base is None else self.resolve_commit(base)
        flags = ["--no-ext-diff", "--no-textconv", "-M", "--no-color"]
        raw = self._run(["diff", "--raw", "-z", "--no-abbrev", *flags, base_ref, target_sha])
        num = self._run(["diff", "--numstat", "-z", *flags, base_ref, target_sha])
        counts = _parse_numstat(num)
        changes: list[FileChange] = []
        for status, path, old_path, mode, oid in _parse_raw(raw):
            adds, dels, binary = counts.get(path, (None, None, False))
            changes.append(
                FileChange(
                    path=path,
                    status=status,
                    old_path=old_path,
                    additions=adds,
                    deletions=dels,
                    binary=binary,
                    oid=None if status == "D" else oid,
                    mode=None if status == "D" else mode,
                )
            )
        return sorted(changes, key=lambda c: c.path)

    def file_diff(
        self, base: object | None, target: object, path: object, *, max_bytes: int = 65536
    ) -> FileDiff:
        """Unified diff of one file. The path must exist in the base or target tree."""
        clean = validate_repo_path(path)
        if base is not None:
            validate_revision(base)
        validate_revision(target)
        target_sha = self.resolve_commit(target)
        base_ref = self._empty_tree() if base is None else self.resolve_commit(base)
        if self.tree_entry(target_sha, clean) is None and (
            base is None or self.tree_entry(base_ref, clean) is None
        ):
            raise RepoError("not_found", "path")
        out, truncated = self._exec(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "-M",
                base_ref,
                target_sha,
                "--",
                clean,
            ],
            max_bytes=max_bytes,
            truncate=True,
        )
        text = out.decode("utf-8", errors="replace")
        binary = "Binary files" in text and "differ" in text
        return FileDiff(clean, "" if binary else text, truncated, binary)

    # ---- trees and blobs ----------------------------------------------------------------

    def ls_tree(self, revision: object) -> list[TreeEntry]:
        sha = self.resolve_commit(revision)
        out = self._run(["ls-tree", "-r", "-z", "--long", sha])
        return [e for e in (_parse_tree_record(r) for r in out.split(b"\x00") if r) if e]

    def tree_entry(self, revision: object, path: object) -> TreeEntry | None:
        """The exact entry for `path` at `revision`, or None. Never matches by prefix or pattern."""
        clean = validate_repo_path(path)
        sha = self.resolve_commit(revision)
        out = self._run(["ls-tree", "-z", "--long", sha, "--", clean])
        for rec in out.split(b"\x00"):
            entry = _parse_tree_record(rec) if rec else None
            if entry is not None and entry.path == clean and entry.type == "blob":
                return entry
        return None

    @contextmanager
    def blob_reader(self, *, timeout: float = 120.0) -> Iterator[_BatchReader]:
        with self._batch(timeout) as reader:
            yield reader


# ---- helpers -----------------------------------------------------------------------------


def _drain(stream: IO[bytes]) -> None:
    try:
        while stream.read(65536):
            pass
    except (OSError, ValueError):
        pass


def _feed(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
        stream.close()
    except (OSError, ValueError):
        pass


class _BatchReader:
    """Reads blobs one at a time from a persistent `git cat-file --batch` process."""

    def __init__(self, proc: subprocess.Popen[bytes], timed_out: threading.Event) -> None:
        self._proc = proc
        self._timed_out = timed_out

    def read(self, oid: str, *, max_bytes: int) -> bytes:
        validate_full_sha(oid)
        assert self._proc.stdin is not None and self._proc.stdout is not None
        try:
            self._proc.stdin.write(oid.encode() + b"\n")
            self._proc.stdin.flush()
            header = self._proc.stdout.readline()
        except OSError:
            header = b""
        if self._timed_out.is_set():
            raise RepoError("timeout")
        fields = header.split()
        if len(fields) == 2 and fields[1] == b"missing":
            raise RepoError("not_found", "object")
        if len(fields) != 3 or fields[1] != b"blob":
            raise RepoError("git_failed", "unexpected_batch_header")
        size = int(fields[2])
        if size > max_bytes:
            remaining = size + 1  # discard payload and newline so the stream stays aligned
            while remaining > 0:
                piece = self._proc.stdout.read(min(65536, remaining))
                if not piece:
                    break
                remaining -= len(piece)
            raise RepoError("snapshot_limit_exceeded", "file_too_large")
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # trailing newline
        if len(data) != size:
            raise RepoError("timeout" if self._timed_out.is_set() else "git_failed", "short_read")
        return data


def _parse_raw(raw: bytes) -> Iterator[tuple[Status, str, str | None, str, str]]:
    tokens = raw.split(b"\x00")
    i = 0
    while i < len(tokens):
        meta = tokens[i]
        i += 1
        if not meta.startswith(b":"):
            continue
        fields = meta[1:].decode().split()
        if len(fields) != 5:
            raise RepoError("git_failed", "unparseable_diff")
        _src_mode, dst_mode, _src_oid, dst_oid, status_raw = fields
        letter = status_raw[0]
        if letter in ("R", "C"):
            old, new = (
                tokens[i].decode("utf-8", "replace"),
                tokens[i + 1].decode("utf-8", "replace"),
            )
            i += 2
            if letter == "R":
                yield "R", new, old, dst_mode, dst_oid
            else:  # copy: treat as an addition of the new path
                yield "A", new, None, dst_mode, dst_oid
            continue
        path = tokens[i].decode("utf-8", "replace")
        i += 1
        if letter in ("A", "M", "D", "T"):
            yield letter, path, None, dst_mode, dst_oid  # type: ignore[misc]
        else:
            raise RepoError("git_failed", "unknown_diff_status")


def _parse_numstat(raw: bytes) -> dict[str, tuple[int | None, int | None, bool]]:
    tokens = raw.split(b"\x00")
    out: dict[str, tuple[int | None, int | None, bool]] = {}
    i = 0
    while i < len(tokens):
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        parts = rec.split(b"\t", 2)
        if len(parts) != 3:
            continue
        adds_raw, dels_raw, path_raw = parts
        if path_raw == b"":  # rename: next two tokens are old and new
            path = tokens[i + 1].decode("utf-8", "replace")
            i += 2
        else:
            path = path_raw.decode("utf-8", "replace")
        binary = adds_raw == b"-"
        out[path] = (
            None if binary else int(adds_raw),
            None if binary else int(dels_raw),
            binary,
        )
    return out


def _parse_tree_record(rec: bytes) -> TreeEntry | None:
    head, sep, path = rec.partition(b"\t")
    if not sep:
        return None
    parts = head.split()
    if len(parts) != 4:
        return None
    mode, typ, oid, size = (p.decode() for p in parts)
    try:
        decoded = path.decode("utf-8")
    except UnicodeDecodeError:
        decoded = path.decode("utf-8", errors="surrogateescape")  # flagged unsafe downstream
    return TreeEntry(mode, typ, oid, -1 if size == "-" else int(size), decoded)
