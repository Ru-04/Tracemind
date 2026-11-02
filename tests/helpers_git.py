"""Helpers that build real temporary Git repositories for tests (no mocks of Git itself)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

AUTHOR_NAME = "Hostile Author"
AUTHOR_EMAIL = "hostile.author@example.test"


class TempRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self._tick = 0
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    def _env(self) -> dict[str, str]:
        self._tick += 1
        stamp = f"2026-10-28T10:{self._tick // 60:02d}:{self._tick % 60:02d}+00:00"
        env = dict(os.environ)
        env.update(
            GIT_AUTHOR_NAME=AUTHOR_NAME,
            GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
            GIT_COMMITTER_NAME=AUTHOR_NAME,
            GIT_COMMITTER_EMAIL=AUTHOR_EMAIL,
            GIT_AUTHOR_DATE=stamp,
            GIT_COMMITTER_DATE=stamp,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        )
        return env

    def git(self, *args: str, input_bytes: bytes | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            input=input_bytes,
            capture_output=True,
            env=self._env(),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {args} failed: {proc.stderr.decode(errors='replace')}")
        return proc.stdout.decode(errors="replace").strip()

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)

    def commit(
        self,
        message: str = "change",
        files: dict[str, str | bytes] | None = None,
        delete: list[str] | None = None,
        rename: dict[str, str] | None = None,
    ) -> str:
        for rel, content in (files or {}).items():
            self.write(rel, content)
        for rel in delete or []:
            self.git("rm", "-q", "--", rel)
        for old, new in (rename or {}).items():
            (self.path / new).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", old, new)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def add_symlink_entry(self, rel: str, target: str) -> None:
        """Record a symlink in the index without needing OS symlink privileges."""
        oid = self.git("hash-object", "-w", "--stdin", input_bytes=target.encode())
        self.git("update-index", "--add", "--cacheinfo", f"120000,{oid},{rel}")

    def add_submodule_entry(self, rel: str, commit: str) -> None:
        self.git("update-index", "--add", "--cacheinfo", f"160000,{commit},{rel}")


def tree_fingerprint(root: Path) -> dict[str, str]:
    """SHA-256 of every file under `root` (including `.git`), for read-only assertions."""
    out: dict[str, str] = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            path = Path(current) / name
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def make_dir_link(link: Path, target: Path) -> bool:
    """Create a symlink, falling back to an NTFS junction. Returns False if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        return result.returncode == 0
    return False
