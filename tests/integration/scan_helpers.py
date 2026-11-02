"""Shared fixtures-as-functions for the scan tests: a wired ScanService on real PostgreSQL, the real
pinned Graphify, a controllable clock, and a fault-injecting runner."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from tracemind.config import Settings
from tracemind.repos.graphify_runner import GraphifyRunner, InstallInfo, RunResult
from tracemind.repos.scan_service import ScanService
from tracemind.repos.scan_store import ScanStore
from tracemind.repos.service import RepositoryService
from tracemind.repos.store import RepositoryStore
from tracemind.storage.postgres import PostgresStore

PROJECT = "project-demo"


class Clock:
    """Settable clock so lease expiry and backoff are tested without sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            self.now += timedelta(milliseconds=1)  # strictly increasing, so orderings are stable
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += timedelta(seconds=seconds)


class FaultyRunner(GraphifyRunner):
    """The real pinned Graphify, with optional faults injected around each call.

    `before(kind, dir, n)` runs first and may raise; `after(kind, dir, n)` runs once Graphify has
    really produced output and may corrupt it."""

    def __init__(
        self,
        real: GraphifyRunner,
        *,
        before: Callable[[str, Path, int], None] | None = None,
        after: Callable[[str, Path, int], None] | None = None,
    ) -> None:
        self._real = real
        self._before = before
        self._after = after
        self.calls: list[tuple[str, Path]] = []
        self._lock = threading.Lock()

    def verify(self) -> InstallInfo:
        return self._real.verify()

    def _go(self, kind: str, d: Path) -> RunResult:
        with self._lock:
            self.calls.append((kind, d))
            n = len(self.calls)
        if self._before:
            self._before(kind, d, n)
        result = self._real.extract(d) if kind == "extract" else self._real.update(d)
        if self._after:
            self._after(kind, d, n)
        return result

    def extract(self, snapshot_dir: Path) -> RunResult:
        return self._go("extract", snapshot_dir)

    def update(self, snapshot_dir: Path) -> RunResult:
        return self._go("update", snapshot_dir)


@dataclass
class Env:
    service: ScanService
    repos: RepositoryService
    scans: ScanStore
    store: RepositoryStore
    engine: sa.Engine
    clock: Clock
    settings: Settings
    data_dir: Path
    root: Path
    real_runner: GraphifyRunner

    def register(self, repo_path: Path, project: str = PROJECT, name: str = "App") -> str:
        return str(self.repos.register(project, name, str(repo_path))["repository_id"])

    def scan(self, repository_id: str, revision: str | None = None, worker: str = "w1"):
        scan, _ = self.service.enqueue(PROJECT, repository_id, revision)
        result = self.service.process_next(worker)
        return scan, result

    def run_all(self, worker: str = "w1") -> list[Any]:
        out: list[Any] = []
        while True:
            res = self.service.process_next(worker)
            if res is None:
                return out
            out.append(res)

    def repo(self, repository_id: str, project: str = PROJECT) -> dict[str, Any]:
        found = self.store.get_repository(project, repository_id)
        assert found is not None
        return found

    def records(self, repository_id: str) -> list[dict[str, Any]]:
        items, _ = self.scans.list_change_records(PROJECT, repository_id, limit=200, cursor=None)
        return list(reversed(items))  # oldest first

    def history(self, scan_id: str) -> list[str]:
        return [e["event"] for e in self.scans.scan_history(PROJECT, scan_id)]

    def current_dir(self, repository_id: str) -> Path:
        return self.data_dir / "repos" / repository_id / "current"

    def state(self, repository_id: str) -> dict[str, Any]:
        """Everything that must survive a failed scan unchanged."""
        repo = self.repo(repository_id)
        root = self.data_dir / "repos" / repository_id
        return {
            "indexed_sha": repo["indexed_sha"],
            "graph_sha256": repo["graph_sha256"],
            "current": fingerprint(root / "current"),
            "graphs": fingerprint(root / "graphs"),
            "current_sha": (root / "current.sha").read_text(encoding="utf-8")
            if (root / "current.sha").exists()
            else None,
            "records": [r["record_id"] for r in self.records(repository_id)],
        }

    def assert_clean(self, repository_id: str) -> None:
        root = self.data_dir / "repos" / repository_id
        for name in ("work", "trash"):
            d = root / name
            assert not d.exists() or list(d.iterdir()) == [], (
                f"{name}/ not cleaned: {list(d.iterdir())}"
            )

    def with_runner(self, runner: GraphifyRunner, **settings: Any) -> ScanService:
        cfg = Settings(**{**self.settings.__dict__, **settings})
        return ScanService(
            repositories=self.repos, scans=self.scans, runner=runner, settings=cfg, clock=self.clock
        )


def fingerprint(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for current, _dirs, files in os.walk(root):
        for name in files:
            p = Path(current) / name
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def build_env(
    engine: sa.Engine,
    tmp_path: Path,
    graphify_bin: Path,
    **settings: Any,
) -> Env:
    root = tmp_path / "repos"
    root.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    clock = Clock()
    cfg = Settings(
        repo_roots=(root,),
        data_dir=data,
        scan_max_attempts=3,
        scan_backoff_seconds=0.0,
        scan_lease_seconds=900.0,
        **settings,
    )
    store = RepositoryStore(engine)
    repos = RepositoryService(store, cfg, clock=clock)
    scans = ScanStore(engine)
    real = GraphifyRunner(graphify_bin, home_dir=data / "graphify-home", timeout=120)
    service = ScanService(repositories=repos, scans=scans, runner=real, settings=cfg, clock=clock)
    return Env(service, repos, scans, store, engine, clock, cfg, data, root, real)


def count(engine: sa.Engine, sql: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.text(sql)).scalar_one())


def graph_sha_of(env: Env, repository_id: str) -> str:
    return str(env.repo(repository_id)["graph_sha256"])


PRICING_V1 = "def average_item_price(total, qty):\n    return total // qty\n\n\ndef regression():\n    return average_item_price(0, 0)\n"
ORDERS_V1 = "from app.pricing import average_item_price\n\n\nclass Orders:\n    def avg(self, items):\n        return average_item_price(1, len(items))\n"


def make_repo(env: Env, name: str = "app"):
    """A small Python+JS repository with one commit, under the allowed root."""
    from tests.helpers_git import TempRepo

    repo = TempRepo(env.root / name)
    repo.commit(
        "initial",
        {
            "app/pricing.py": PRICING_V1,
            "app/orders.py": ORDERS_V1,
            "web/util.js": "export function fmt(x) { return String(x); }\n",
            "README.md": "docs\n",
        },
    )
    return repo


# Re-exported for tests
PostgresStore = PostgresStore
