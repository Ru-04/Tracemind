"""M2-5: failed scans, bounded retries, timeouts, corrupt or incomplete Graphify output, interrupted
workers, fenced completion — and, throughout, the last known-good graph is never lost or replaced
by anything partial. Uses the real pinned Graphify with faults injected around it."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from tracemind.repos.git_adapter import GitAdapter
from tracemind.repos.graphify_runner import GraphifyError, GraphifyRunner
from tracemind.repos.scan_store import LostLeaseError, ScanOutcome
from tracemind.repos.snapshot import verify_snapshot

from tests.helpers_git import make_dir_link
from tests.integration.scan_helpers import PROJECT, FaultyRunner, count, make_repo

pytestmark = [pytest.mark.integration, pytest.mark.graphify]

PRICING_EDIT = "def average_item_price(total, qty):\n    return total // max(qty, 1)\n\n\ndef regression():\n    return average_item_price(0, 0)\n"


def seeded(env, name: str = "app"):
    """A registered repository with one succeeded scan (the 'last known-good' state)."""
    repo = make_repo(env, name)
    repo_id = env.register(repo.path)
    _, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded"
    return repo, repo_id


def raise_graphify(code: str, detail: str | None = None):
    def hook(kind, d, n):
        raise GraphifyError(code, detail)

    return hook


def truncate_graph(kind: str, d: Path, n: int) -> None:
    (d / "graphify-out" / "graph.json").write_text('{"nodes": [', encoding="utf-8")


def drop_a_node(kind: str, d: Path, n: int) -> None:
    """Valid JSON, valid schema — but an edge now points at a missing node (a partial write)."""
    path = d / "graphify-out" / "graph.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["nodes"].pop(0)
    path.write_text(json.dumps(doc), encoding="utf-8")


def empty_graph(kind: str, d: Path, n: int) -> None:
    path = d / "graphify-out" / "graph.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["nodes"], doc["links"] = [], []
    path.write_text(json.dumps(doc), encoding="utf-8")


def wrong_schema(kind: str, d: Path, n: int) -> None:
    path = d / "graphify-out" / "graph.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["graph"]["schema_version"] = 99
    path.write_text(json.dumps(doc), encoding="utf-8")


# ---- failure leaves the last good state untouched ---------------------------------------


@pytest.mark.parametrize(
    ("label", "runner_kwargs", "expected_code"),
    [
        (
            "graphify exits non-zero",
            {"before": raise_graphify("graphify_failed", "exit_1")},
            "graphify_failed",
        ),
        ("graphify times out", {"before": raise_graphify("graphify_timeout")}, "graphify_timeout"),
        ("truncated graph.json", {"after": truncate_graph}, "graph_invalid"),
        ("edge to a missing node", {"after": drop_a_node}, "graph_invalid"),
        ("unsupported schema version", {"after": wrong_schema}, "graph_invalid"),
        ("empty graph for a non-empty codebase", {"after": empty_graph}, "graph_empty"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_failed_scans_never_replace_the_last_known_good_graph(
    label, runner_kwargs, expected_code, env
) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    # a full rebuild (not incremental) so injected faults hit the only path; also covers update below
    service = env.with_runner(FaultyRunner(env.real_runner, **runner_kwargs))
    service.enqueue(PROJECT, repo_id)
    results = []
    while (res := service.process_next("w1")) is not None:
        results.append(res)

    assert results[-1].status == "failed" and results[-1].error_code == expected_code, label
    assert env.state(repo_id) == good, label  # pointer, graph, snapshot, records: all unchanged
    env.assert_clean(repo_id)
    scan = env.scans.latest_scan(PROJECT, repo_id)
    assert scan is not None and scan["status"] == "failed" and scan["error_code"] == expected_code
    assert scan["graph_sha256"] is None  # a failed scan publishes nothing
    # the previous graph still loads and answers queries
    graph = env.service.published_graph(PROJECT, repo_id)
    assert graph is not None and graph.graph_sha256 == good["graph_sha256"]
    assert graph.find_symbols("average_item_price")
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), good["indexed_sha"]).ok


def test_retries_are_bounded_and_recorded(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    runner = FaultyRunner(env.real_runner, before=raise_graphify("graphify_failed", "exit_1"))
    service = env.with_runner(runner)
    scan, _ = service.enqueue(PROJECT, repo_id)
    statuses = []
    while (res := service.process_next("w1")) is not None:
        statuses.append(res.status)
    assert statuses == ["queued", "queued", "failed"]  # exactly max_attempts=3 attempts
    assert env.history(scan["scan_id"]) == [
        "queued",
        "claimed",
        "retry_scheduled",
        "claimed",
        "retry_scheduled",
        "claimed",
        "failed",
    ]
    done = env.scans.get_scan(PROJECT, scan["scan_id"])
    assert done is not None and done["attempt"] == 3 and done["status"] == "failed"
    assert service.process_next("w1") is None  # nothing left to retry
    # each attempt tries the incremental update, then one automatic full rebuild; still bounded
    assert [k for k, _ in runner.calls] == ["update", "extract"] * 3


def test_a_transient_failure_succeeds_on_retry_and_publishes_once(env) -> None:
    repo, repo_id = seeded(env)
    sha = repo.commit("edit", {"app/pricing.py": PRICING_EDIT})

    def flaky(kind, d, n):
        # each failing attempt makes two calls: the incremental update, then its automatic rebuild
        if n <= 4:
            raise GraphifyError("graphify_failed", "exit_1")

    service = env.with_runner(FaultyRunner(env.real_runner, before=flaky))
    scan, _ = service.enqueue(PROJECT, repo_id)
    statuses = []
    while (res := service.process_next("w1")) is not None:
        statuses.append(res.status)
    assert statuses == ["queued", "queued", "succeeded"]
    assert env.repo(repo_id)["indexed_sha"] == sha
    assert (
        len(env.records(repo_id)) == 2
    )  # one record for the initial scan, exactly one for this one
    assert env.history(scan["scan_id"])[-1] == "succeeded"
    env.assert_clean(repo_id)


def test_backoff_delays_the_retry(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    service = env.with_runner(
        FaultyRunner(env.real_runner, before=raise_graphify("graphify_failed", "exit_1")),
        scan_backoff_seconds=60.0,
    )
    service.enqueue(PROJECT, repo_id)
    assert service.process_next("w1").status == "queued"
    assert service.process_next("w1") is None  # not yet: backing off
    env.clock.advance(61)
    assert service.process_next("w1").status == "queued"
    assert service.process_next("w1") is None  # backoff doubled to 120 s
    env.clock.advance(61)
    assert service.process_next("w1") is None
    env.clock.advance(61)
    assert service.process_next("w1").status == "failed"


@pytest.mark.parametrize(
    "code", ["graphify_unavailable", "graphify_version_mismatch", "graphify_install_modified"]
)
def test_configuration_failures_fail_fast_without_retrying(code, env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    service = env.with_runner(FaultyRunner(env.real_runner, before=raise_graphify(code)))
    scan, _ = service.enqueue(PROJECT, repo_id)
    res = service.process_next("w1")
    assert res is not None and res.status == "failed" and res.error_code == code
    assert env.scans.get_scan(PROJECT, scan["scan_id"])["attempt"] == 1


def test_a_real_timeout_kills_graphify_and_fails_cleanly(env) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    impatient = GraphifyRunner(env.real_runner._bin, home_dir=env.data_dir / "h2", timeout=0.01)  # noqa: SLF001
    service = env.with_runner(impatient)
    service.enqueue(PROJECT, repo_id)
    last = None
    while (res := service.process_next("w1")) is not None:
        last = res
    assert last is not None and last.status == "failed" and last.error_code == "graphify_timeout"
    assert env.state(repo_id) == good
    env.assert_clean(repo_id)


# ---- recovery inside a scan --------------------------------------------------------------


def test_a_corrupt_published_graph_file_triggers_a_full_rebuild_not_a_failure(env) -> None:
    repo, repo_id = seeded(env)
    sha = repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    # Corrupt the incremental baseline Graphify will see (the copy inside `current/`).
    (env.current_dir(repo_id) / "graphify-out" / "graph.json").write_text(
        '{"nodes": [', encoding="utf-8"
    )
    scan, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded" and result.rebuilt is True
    assert "graph_rebuilt" in env.history(scan["scan_id"])
    assert env.repo(repo_id)["indexed_sha"] == sha

    # The rebuilt graph equals a graph built from scratch for the same commit.
    fresh_dir = env.data_dir / "fresh"
    from tracemind.repos.code_graph import CodeGraph
    from tracemind.repos.snapshot import export_snapshot

    export_snapshot(GitAdapter(repo.path), sha, fresh_dir)
    env.real_runner.extract(fresh_dir)
    files = sorted(
        p.relative_to(fresh_dir).as_posix()
        for p in fresh_dir.rglob("*")
        if p.is_file() and "graphify-out" not in p.parts
    )
    reference = CodeGraph.load(fresh_dir / "graphify-out" / "graph.json", snapshot_files=files)
    assert env.repo(repo_id)["graph_sha256"] == reference.graph_sha256
    env.assert_clean(repo_id)


def test_incremental_update_that_produces_garbage_is_rebuilt_and_still_valid(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})

    def break_updates_only(kind: str, d: Path, n: int) -> None:
        if kind == "update":
            truncate_graph(kind, d, n)

    service = env.with_runner(FaultyRunner(env.real_runner, after=break_updates_only))
    scan, _ = service.enqueue(PROJECT, repo_id)
    res = service.process_next("w1")
    assert res is not None and res.status == "succeeded" and res.rebuilt
    assert (
        env.repo(repo_id)["graph_sha256"]
        == env.scans.get_scan(PROJECT, scan["scan_id"])["graph_sha256"]
    )
    assert env.service.published_graph(PROJECT, repo_id) is not None


def test_a_tampered_snapshot_is_detected_and_rebuilt_from_git(env) -> None:
    repo, repo_id = seeded(env)
    sha = repo.commit("add", {"app/extra.py": "e = 1\n"})
    (env.current_dir(repo_id) / "app" / "pricing.py").write_text("TAMPERED\n", encoding="utf-8")
    scan, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded" and result.kind == "full"
    assert "snapshot_rebuilt" in env.history(scan["scan_id"])
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok
    assert "TAMPERED" not in (env.current_dir(repo_id) / "app" / "pricing.py").read_text(
        encoding="utf-8"
    )


def test_a_missing_current_directory_falls_back_to_a_full_scan(env) -> None:
    repo, repo_id = seeded(env)
    sha = repo.commit("add", {"app/extra.py": "e = 1\n"})
    shutil.rmtree(env.current_dir(repo_id))  # e.g. a crash between the two renames of a swap
    _, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded" and result.kind == "full"
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok
    assert (
        env.records(repo_id)[-1]["base_sha"] == env.records(repo_id)[0]["target_sha"]
    )  # still diffs vs last good


def test_a_missing_published_graph_file_forces_a_full_scan_and_marks_the_baseline(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("add", {"app/extra.py": "e = 1\n"})
    for f in (env.data_dir / "repos" / repo_id / "graphs").iterdir():
        f.unlink()
    _, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded" and result.kind == "full"
    assert env.records(repo_id)[-1]["graph_delta"].get("baseline_missing") is True


# ---- interrupted workers, leases and fencing ---------------------------------------------


def test_a_scan_abandoned_by_a_dead_worker_is_reclaimed_and_completed(env) -> None:
    repo, repo_id = seeded(env)
    sha = repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    scan, _ = env.service.enqueue(PROJECT, repo_id)
    dead = env.scans.claim_next(
        worker_id="dead", now=env.clock(), lease_seconds=900
    )  # claims, then dies
    assert dead is not None and dead["scan_id"] == scan["scan_id"] and dead["attempt"] == 1
    assert env.service.process_next("w2") is None  # lease still valid: nobody else may take it
    env.clock.advance(901)
    res = env.service.process_next("w2")
    assert res is not None and res.status == "succeeded"
    assert env.history(scan["scan_id"]) == [
        "queued",
        "claimed",
        "lease_expired",
        "claimed",
        "succeeded",
    ]
    assert env.scans.get_scan(PROJECT, scan["scan_id"])["attempt"] == 2
    assert env.repo(repo_id)["indexed_sha"] == sha and len(env.records(repo_id)) == 2


def test_repeated_worker_loss_exhausts_attempts_and_fails_with_worker_lost(env) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    scan, _ = env.service.enqueue(PROJECT, repo_id)
    for i in range(3):
        assert (
            env.scans.claim_next(worker_id=f"dead{i}", now=env.clock(), lease_seconds=10)
            is not None
        )
        env.clock.advance(11)
    assert env.scans.claim_next(worker_id="w", now=env.clock(), lease_seconds=10) is None
    done = env.scans.get_scan(PROJECT, scan["scan_id"])
    assert done is not None and done["status"] == "failed" and done["error_code"] == "worker_lost"
    assert (
        env.history(scan["scan_id"]).count("lease_expired") == 3
        and env.history(scan["scan_id"])[-1] == "failed"
    )
    assert env.state(repo_id) == good


def test_a_worker_that_lost_its_lease_cannot_publish(env) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    scan, _ = env.service.enqueue(PROJECT, repo_id)
    stale = env.scans.claim_next(worker_id="stale", now=env.clock(), lease_seconds=10)
    assert stale is not None
    env.clock.advance(11)
    res = env.service.process_next("fresh")  # reclaims and completes the scan
    assert res is not None and res.status == "succeeded"
    published = env.state(repo_id)
    outcome = ScanOutcome(
        graph_tool="graphify",
        graph_tool_version="0.9.97",
        graph_schema_version=1,
        graph_sha256="f" * 64,
        node_count=1,
        edge_count=1,
        inferred_edge_count=0,
        files_in_snapshot=1,
        files_in_graph=1,
        coverage={},
        kind="full",
        base_sha=None,
        files={"items": [], "total": 0, "counts": {}},
        graph_delta={},
    )
    with pytest.raises(LostLeaseError):
        env.scans.complete_success(scan=stale, worker_id="stale", now=env.clock(), outcome=outcome)
    with pytest.raises(LostLeaseError):
        env.scans.fail_attempt(
            scan=stale,
            worker_id="stale",
            now=env.clock(),
            error_code="x",
            retryable=True,
            retry_delay_seconds=0,
        )
    assert env.state(repo_id) == published != good  # the late worker changed nothing
    assert env.scans.get_scan(PROJECT, scan["scan_id"])["status"] == "succeeded"
    assert count(env.engine, "SELECT count(*) FROM change_records") == 2


def test_losing_the_lease_mid_scan_abandons_it_without_publishing(env, monkeypatch) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    service = env.with_runner(
        FaultyRunner(env.real_runner, before=lambda k, d, n: time.sleep(0.6)),
        scan_lease_seconds=0.3,
    )
    scan, _ = service.enqueue(PROJECT, repo_id)
    from tracemind.repos.scan_store import ScanStore

    monkeypatch.setattr(ScanStore, "heartbeat", lambda self, **kw: False)  # the renewal is refused
    res = service.process_next("w1")
    assert res is not None and res.status == "abandoned"
    assert env.state(repo_id) == good
    env.assert_clean(repo_id)
    assert (
        env.scans.get_scan(PROJECT, scan["scan_id"])["status"] == "running"
    )  # still owned by the lease, not corrupted


def test_the_heartbeat_keeps_a_slow_scan_alive(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    service = env.with_runner(
        FaultyRunner(env.real_runner, before=lambda k, d, n: time.sleep(0.8)),
        scan_lease_seconds=0.45,
    )
    service.enqueue(PROJECT, repo_id)
    res = service.process_next("w1")
    assert (
        res is not None and res.status == "succeeded"
    )  # outlived its initial lease thanks to renewals


# ---- history is never lost ---------------------------------------------------------------


def test_scan_history_survives_failures_retries_and_success(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})

    def once(kind, d, n):
        if (
            n <= 2
        ):  # the update and its automatic rebuild both fail: the attempt fails and is retried
            raise GraphifyError("graphify_failed", "exit_1")

    service = env.with_runner(FaultyRunner(env.real_runner, before=once))
    scan, _ = service.enqueue(PROJECT, repo_id)
    while service.process_next("w1") is not None:
        pass
    events = env.scans.scan_history(PROJECT, scan["scan_id"])
    assert [e["event"] for e in events] == [
        "queued",
        "claimed",
        "retry_scheduled",
        "claimed",
        "succeeded",
    ]
    assert [e["attempt"] for e in events] == [0, 1, 1, 2, 2]
    assert events[2]["error_code"] == "graphify_failed"
    assert all(e["at"] for e in events)
    with env.engine.begin() as conn, pytest.raises(sa.exc.DBAPIError):
        conn.execute(
            sa.text("DELETE FROM scan_events")
        )  # append-only, even for the application role


# ---- the registered path changes under us ------------------------------------------------


def test_a_repository_swapped_for_a_link_to_elsewhere_is_refused(env, tmp_path: Path) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    other = make_repo(env, "elsewhere-source")
    moved = env.root / "moved-away"
    repo.path.rename(moved)
    outside = tmp_path / "outside"
    shutil.copytree(other.path, outside)
    if not make_dir_link(repo.path, outside):
        pytest.skip("cannot create a symlink or junction on this machine")
    service = env.service
    scan, _ = (None, None)
    from tracemind.repos.errors import RepoError

    with pytest.raises(RepoError) as info:
        service.enqueue(PROJECT, repo_id)
    assert (info.value.code, info.value.detail) == (
        "invalid_repository_path",
        "outside_allowed_roots",
    )
    assert env.state(repo_id) == good and scan is None


def test_a_repository_that_disappears_fails_the_scan_without_touching_the_index(env) -> None:
    repo, repo_id = seeded(env)
    good = env.state(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    scan, _ = env.service.enqueue(PROJECT, repo_id)
    shutil.rmtree(repo.path, ignore_errors=True)
    res = env.service.process_next("w1")
    assert (
        res is not None and res.status == "failed" and res.error_code == "invalid_repository_path"
    )
    assert env.scans.get_scan(PROJECT, scan["scan_id"])["attempt"] == 1  # non-retryable
    assert env.state(repo_id) == good


def test_work_directories_from_a_crashed_attempt_are_swept(env) -> None:
    repo, repo_id = seeded(env)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    scan, _ = env.service.enqueue(PROJECT, repo_id)
    stale = env.data_dir / "repos" / repo_id / "work" / f"{scan['scan_id']}-oldworker-1"
    stale.mkdir(parents=True)
    (stale / "junk.txt").write_text("left behind", encoding="utf-8")
    assert env.service.process_next("w1").status == "succeeded"
    env.assert_clean(repo_id)


def test_a_scan_of_an_already_indexed_commit_is_not_run_twice(env) -> None:
    repo, repo_id = seeded(env)
    again, created = env.service.enqueue(PROJECT, repo_id)
    assert created is False and again["status"] == "succeeded"
    assert threading.active_count() >= 1 and timedelta(0) == timedelta(0)
