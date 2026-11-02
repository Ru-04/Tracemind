"""Concurrency: duplicate enqueues, simultaneous claims, parallel workers, per-repository
serialization — on real PostgreSQL with the real pinned Graphify."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from tracemind.worker import ScanWorker

from tests.integration.scan_helpers import PROJECT, FaultyRunner, count, make_repo

pytestmark = [pytest.mark.integration, pytest.mark.graphify]


def test_concurrent_identical_enqueues_create_exactly_one_scan(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    created: list[bool] = []
    ids: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        try:
            barrier.wait()
            scan, was_created = env.service.enqueue(PROJECT, repo_id)
            created.append(was_created)
            ids.append(scan["scan_id"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert created.count(True) == 1 and len(set(ids)) == 1
    assert count(env.engine, "SELECT count(*) FROM repository_scans") == 1


def test_two_simultaneous_claims_never_receive_the_same_scan(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.service.enqueue(PROJECT, repo_id)
    got: list[str] = []
    barrier = threading.Barrier(8)

    def claimer(i: int) -> None:
        barrier.wait()
        claimed = env.scans.claim_next(worker_id=f"w{i}", now=env.clock(), lease_seconds=900)
        if claimed is not None:
            got.append(claimed["locked_by"])

    threads = [threading.Thread(target=claimer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 1
    assert env.history(env.scans.latest_scan(PROJECT, repo_id)["scan_id"]).count("claimed") == 1


def test_parallel_workers_process_every_scan_once_and_serialize_per_repository(env) -> None:
    repos = []
    for i in range(3):
        repo = make_repo(env, f"app{i}")
        repo_id = env.register(repo.path, name=f"App {i}")
        first = repo.git("rev-parse", "HEAD")
        env.service.enqueue(PROJECT, repo_id, first)
        second = repo.commit(
            "edit",
            {"app/pricing.py": f"def average_item_price(t, q):\n    return t // (q or {i + 1})\n"},
        )
        env.service.enqueue(PROJECT, repo_id, second)
        repos.append((repo_id, first, second))
    assert count(env.engine, "SELECT count(*) FROM repository_scans") == 6

    active: Counter[str] = Counter()
    peak_per_repo: Counter[str] = Counter()
    overall = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def enter(kind: str, d: Path, n: int) -> None:
        repo_id = d.parent.parent.name
        with lock:
            active[repo_id] += 1
            overall["now"] += 1
            peak_per_repo[repo_id] = max(peak_per_repo[repo_id], active[repo_id])
            overall["peak"] = max(overall["peak"], overall["now"])
        time.sleep(0.25)  # widen the window in which a second worker could wrongly overlap

    def leave(kind: str, d: Path, n: int) -> None:
        with lock:
            active[d.parent.parent.name] -= 1
            overall["now"] -= 1

    service = env.with_runner(FaultyRunner(env.real_runner, before=enter, after=leave))
    results: list[str] = []
    errors: list[BaseException] = []

    def work(name: str) -> None:
        try:
            while True:
                res = service.process_next(name)
                if res is None:
                    # a scan may be waiting only for its repository's other scan to finish
                    if (
                        count(
                            env.engine,
                            "SELECT count(*) FROM repository_scans WHERE status IN ('queued','running')",
                        )
                        == 0
                    ):
                        return
                    time.sleep(0.05)
                    continue
                results.append(res.status)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    assert not errors, errors
    assert results.count("succeeded") == 6 and len(results) == 6

    assert max(peak_per_repo.values()) == 1  # never two scans of one repository at once
    assert overall["peak"] >= 2  # but different repositories did run in parallel
    for repo_id, first, second in repos:
        records = env.records(repo_id)
        assert [(r["base_sha"], r["target_sha"]) for r in records] == [
            (None, first),
            (first, second),
        ]
        assert env.repo(repo_id)["indexed_sha"] == second
        env.assert_clean(repo_id)
    with env.engine.connect() as conn:
        import sqlalchemy as sa

        attempts = [r[0] for r in conn.execute(sa.text("SELECT attempt FROM repository_scans"))]
    assert attempts == [1] * 6  # no scan was ever claimed twice


def test_the_worker_loop_processes_queued_scans_and_stops_on_request(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    scan, _ = env.service.enqueue(PROJECT, repo_id)
    worker = ScanWorker(env.service, worker_id="loop-worker", poll_interval=0.05)
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    deadline = time.time() + 60
    while time.time() < deadline:
        done = env.scans.get_scan(PROJECT, scan["scan_id"])
        if done and done["status"] == "succeeded":
            break
        time.sleep(0.1)
    worker.stop_event.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert env.scans.get_scan(PROJECT, scan["scan_id"])["status"] == "succeeded"
    assert "loop-worker" in {
        e["worker_id"] for e in env.scans.scan_history(PROJECT, scan["scan_id"])
    }


def test_the_worker_survives_an_unexpected_error_in_one_iteration(env, monkeypatch) -> None:
    worker = ScanWorker(env.service, worker_id="resilient", poll_interval=0.05)
    calls = {"n": 0}

    def boom(worker_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup postgresql://u:secret@h/db")
        worker.stop_event.set()
        return None

    monkeypatch.setattr(env.service, "process_next", boom)
    worker.run_forever()
    assert calls["n"] == 2  # kept running after the first iteration raised
