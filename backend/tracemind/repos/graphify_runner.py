"""Runs the pinned, isolated Graphify CLI (ADR-0003). Nothing else in TraceMind runs it.

- The binary must live in its own virtual environment; before use, the installed package is
  verified against its own RECORD hashes and the pinned version (`verify`).
- Only two invocations exist: `extract . --code-only --no-gitignore` and `update .`, always with
  the snapshot directory as working directory (graph paths are then snapshot-relative; Graphify
  otherwise records paths relative to wherever it was started).
- Fixed argv, no shell. A scrubbed environment: no API keys, credentials or inherited proxies;
  deny-all proxy variables so any stray network attempt fails at once; HOME and profile
  directories pointed at a TraceMind-owned scratch directory.
- A wall-clock timeout that kills the whole process tree, and a bounded output capture.
- Exit status 0 is necessary but **not** sufficient: callers must validate `graph.json`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PINNED_VERSION = "0.9.97"
PINNED_WHEEL_SHA256 = "eb24dd7f93293753c001ca36e7304e0b52e86fe4124c2f531dcdb3d64865e01a"
MAX_CAPTURE = 64 * 1024

_VERIFY_SCRIPT = """
import base64, hashlib, importlib.metadata as m, json
d = m.distribution("graphifyy")
bad, checked = [], 0
for f in d.files or []:
    name = str(f).replace(chr(92), "/")
    if f.hash is None or "__pycache__" in name or name.endswith(".pyc"):
        continue
    data = d.locate_file(f).read_bytes()
    got = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    checked += 1
    if got != f.hash.value:
        bad.append(name)
print(json.dumps({"version": d.version, "checked": checked, "bad": bad[:20]}))
"""


class GraphifyError(Exception):
    """`code` is stable: graphify_unavailable, graphify_version_mismatch, graphify_install_modified,
    graphify_timeout, graphify_failed. Messages never include repository content."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if detail is None else f"{code}:{detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    duration_seconds: float
    output_tail: str  # bounded; may include file names, never used in API responses


@dataclass(frozen=True)
class InstallInfo:
    version: str
    files_verified: int


def _scrubbed_env(bin_path: Path, home: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "SYSTEMROOT",
        "SystemRoot",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    system_path = os.environ.get("SYSTEMROOT", "")
    env["PATH"] = os.pathsep.join(
        p
        for p in (
            str(bin_path.parent),
            os.path.join(system_path, "System32") if system_path else "",
        )
        if p
    )
    home.mkdir(parents=True, exist_ok=True)
    for key in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    ):
        env[key] = str(home)
    deny = "http://127.0.0.1:9"  # nothing listens on the discard port: network use fails fast
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = deny
    env["NO_PROXY"] = ""
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(  # noqa: S603, S607 - fixed argv
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False
        )
    else:  # pragma: no cover - exercised on POSIX only
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


class GraphifyRunner:
    def __init__(
        self,
        bin_path: Path,
        *,
        home_dir: Path,
        timeout: float = 300.0,
        expected_version: str = PINNED_VERSION,
    ) -> None:
        self._bin = bin_path
        self._home = home_dir
        self._timeout = timeout
        self._expected = expected_version
        self._verified: InstallInfo | None = None

    # ---- installation check -------------------------------------------------------------

    def _python(self) -> Path:
        return self._bin.parent / ("python.exe" if os.name == "nt" else "python")

    def verify(self) -> InstallInfo:
        """The CLI exists, is the pinned version, and its installed files match their hashes."""
        if self._verified is not None:
            return self._verified
        if not self._bin.is_file() or not self._python().is_file():
            raise GraphifyError("graphify_unavailable")
        proc = subprocess.run(  # noqa: S603 - fixed script, no caller input
            [str(self._python()), "-I", "-c", _VERIFY_SCRIPT],
            capture_output=True,
            env=_scrubbed_env(self._bin, self._home),
            timeout=60,
            check=False,
        )
        try:
            info = json.loads(proc.stdout.decode("utf-8", errors="replace"))
            version, checked, bad = str(info["version"]), int(info["checked"]), list(info["bad"])
        except (ValueError, KeyError, TypeError):
            raise GraphifyError("graphify_unavailable", "verification_failed") from None
        if version != self._expected:
            raise GraphifyError("graphify_version_mismatch")
        if bad or checked == 0:
            raise GraphifyError("graphify_install_modified")
        self._verified = InstallInfo(version, checked)
        return self._verified

    # ---- the only two commands ----------------------------------------------------------

    def extract(self, snapshot_dir: Path) -> RunResult:
        return self._run(["extract", ".", "--code-only", "--no-gitignore"], snapshot_dir)

    def update(self, snapshot_dir: Path) -> RunResult:
        return self._run(["update", "."], snapshot_dir)

    def _run(self, args: list[str], snapshot_dir: Path) -> RunResult:
        self.verify()
        if not snapshot_dir.is_dir():
            raise GraphifyError("graphify_failed", "snapshot_missing")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        started = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [str(self._bin), *args],
            cwd=snapshot_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_scrubbed_env(self._bin, self._home),
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
        assert proc.stdout is not None
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            _kill_tree(proc)

        timer = threading.Timer(self._timeout, _expire)
        timer.start()
        captured = bytearray()
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                captured += chunk
                if len(captured) > MAX_CAPTURE:
                    del captured[: len(captured) - MAX_CAPTURE]  # keep the tail, keep draining
        finally:
            timer.cancel()
            proc.wait()
            proc.stdout.close()
        if timed_out.is_set():
            raise GraphifyError("graphify_timeout")
        result = RunResult(
            proc.returncode,
            time.monotonic() - started,
            captured.decode("utf-8", errors="replace"),
        )
        if proc.returncode != 0:
            raise GraphifyError("graphify_failed", f"exit_{proc.returncode}")
        return result


Command = Literal["extract", "update"]
