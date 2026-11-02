"""M2-2, M2-3 (record side), M2-4, M2-8: initial and incremental scans on a real repository with
the real pinned Graphify, change records, idempotence, read-only guarantees."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
from typing import Any

import pytest
from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import GitAdapter
from tracemind.repos.snapshot import verify_snapshot

from tests.helpers_git import AUTHOR_EMAIL, AUTHOR_NAME, tree_fingerprint
from tests.integration.scan_helpers import PROJECT, count, make_repo

pytestmark = [pytest.mark.integration, pytest.mark.graphify]


def test_initial_scan_records_identity_sha_graph_version_and_status(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    head = repo.git("rev-parse", "HEAD")
    scan, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded" and result.kind == "full"

    done = env.scans.get_scan(PROJECT, scan["scan_id"])
    assert done is not None
    assert (done["status"], done["kind"], done["target_sha"], done["base_sha"]) == (
        "succeeded",
        "full",
        head,
        None,
    )
    assert (done["graph_tool"], done["graph_tool_version"], done["graph_schema_version"]) == (
        "graphify",
        "0.9.97",
        1,
    )
    assert len(done["graph_sha256"]) == 64 and done["node_count"] > 0 and done["edge_count"] > 0
    assert done["files_in_snapshot"] == 4 and done["files_in_graph"] == 3
    assert done["coverage"]["complete"] is True and done["coverage"]["not_assessed_files"] == 1
    assert done["attempt"] == 1 and done["error_code"] is None and done["finished_at"]

    state = env.repo(repo_id)
    assert state["indexed_sha"] == head and state["graph_sha256"] == done["graph_sha256"]
    assert state["identity"] == "root:" + repo.git("rev-list", "--max-parents=0", "HEAD")
    assert env.history(scan["scan_id"]) == ["queued", "claimed", "succeeded"]
    env.assert_clean(repo_id)


def test_the_snapshot_is_exactly_the_commit_and_the_published_graph_matches_its_hash(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    (repo.path / "app" / "pricing.py").write_text(
        "UNCOMMITTED\n", encoding="utf-8"
    )  # must be ignored
    env.scan(repo_id)
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), "HEAD").ok
    graph = env.service.published_graph(PROJECT, repo_id)
    assert graph is not None and graph.graph_sha256 == env.repo(repo_id)["graph_sha256"]
    assert "UNCOMMITTED" not in (env.current_dir(repo_id) / "app" / "pricing.py").read_text(
        encoding="utf-8"
    )


def test_initial_change_record_lists_every_file_as_an_addition_and_cites_real_commits(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    (record,) = env.records(repo_id)
    head = repo.git("rev-parse", "HEAD")
    assert (record["base_sha"], record["target_sha"], record["commits"]) == (None, head, [head])
    assert record["files"]["counts"] == {"A": 4} and record["files"]["total"] == 4
    assert {f["path"] for f in record["files"]["items"]} == {
        "app/pricing.py",
        "app/orders.py",
        "web/util.js",
        "README.md",
    }
    assert record["graph_delta"]["nodes_added"] > 0 and record["graph_delta"]["nodes_removed"] == 0
    assert (
        record["graph_sha256"] == env.repo(repo_id)["graph_sha256"]
        and len(record["content_hash"]) == 64
    )


def test_retriggering_the_same_commit_is_idempotent(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    first, _ = env.scan(repo_id)
    again, created = env.service.enqueue(PROJECT, repo_id)
    assert (
        created is False and again["scan_id"] == first["scan_id"] and again["status"] == "succeeded"
    )
    assert env.service.process_next("w1") is None
    assert count(env.engine, "SELECT count(*) FROM repository_scans") == 1
    assert count(env.engine, "SELECT count(*) FROM change_records") == 1


def test_the_same_commit_scanned_independently_gives_the_same_graph_hash(env, tmp_path) -> None:
    repo = make_repo(env)
    first_id = env.register(repo.path)
    env.scan(first_id)
    # A completely separate data directory and project: nothing is shared with the first scan.
    other = env.with_runner(env.real_runner, data_dir=tmp_path / "other-data")
    second_id = str(env.repos.register("project-other", "Again", str(repo.path))["repository_id"])
    scan, _ = other.enqueue("project-other", second_id)
    assert other.process_next("w2") is not None
    done = env.scans.get_scan("project-other", scan["scan_id"])
    assert done is not None and done["status"] == "succeeded"
    assert done["graph_sha256"] == env.repo(first_id)["graph_sha256"]


# ---- M2-4: add / edit / delete / rename each update the index and create ONE change record --


def changed(record: dict[str, Any], status: str) -> set[str]:
    return {f["path"] for f in record["files"]["items"] if f["status"] == status}


def test_file_addition(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    before = env.repo(repo_id)["graph_sha256"]
    sha = repo.commit(
        "add audit",
        {
            "app/audit.py": "from app.pricing import regression\n\n\ndef audit():\n    return regression()\n"
        },
    )
    scan, result = env.scan(repo_id)
    assert result is not None and result.status == "succeeded" and result.kind == "incremental"
    record = env.records(repo_id)[-1]
    assert (record["base_sha"], record["target_sha"], record["commits"]) == (
        env.records(repo_id)[0]["target_sha"],
        sha,
        [sha],
    )
    assert changed(record, "A") == {"app/audit.py"} and record["files"]["total"] == 1
    assert "app/audit.py:audit()" in record["graph_delta"]["added_symbols"]
    assert record["graph_delta"]["removed_symbols"] == []
    assert env.repo(repo_id)["indexed_sha"] == sha and env.repo(repo_id)["graph_sha256"] != before
    graph = env.service.published_graph(PROJECT, repo_id)
    assert graph is not None and "app/audit.py" in graph.files()
    (regression,) = graph.find_symbols("regression")
    assert any(
        r["file"] == "app/audit.py" for r in graph.symbol_context(regression)["callers"]["items"]
    )
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok
    assert count(env.engine, "SELECT count(*) FROM change_records") == 2
    env.assert_clean(repo_id)


def test_file_edit(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    sha = repo.commit(
        "guard",
        {
            "app/pricing.py": "def average_item_price(total, qty):\n    return total // qty if qty else 0\n\n\ndef regression():\n    return average_item_price(0, 0)\n\n\ndef extra():\n    return regression()\n"
        },
    )
    _, result = env.scan(repo_id)
    assert result is not None and result.kind == "incremental"
    record = env.records(repo_id)[-1]
    assert changed(record, "M") == {"app/pricing.py"} and record["files"]["total"] == 1
    item = record["files"]["items"][0]
    adds, dels, _ = repo.git("diff", "--numstat", "HEAD~1", "HEAD").split(
        "	"
    )  # Git is the authority
    assert (item["additions"], item["deletions"]) == (int(adds), int(dels))
    assert record["graph_delta"]["added_symbols"] == ["app/pricing.py:extra()"]
    assert env.repo(repo_id)["indexed_sha"] == sha
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok


def test_file_deletion(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    sha = repo.commit("drop js", delete=["web/util.js"])
    env.scan(repo_id)
    record = env.records(repo_id)[-1]
    assert changed(record, "D") == {"web/util.js"}
    assert (
        record["graph_delta"]["removed_symbols"] == ["web/util.js:fmt()"]
        and record["graph_delta"]["added_symbols"] == []
    )
    graph = env.service.published_graph(PROJECT, repo_id)
    assert graph is not None and "web/util.js" not in graph.files()
    assert not (env.current_dir(repo_id) / "web").exists()
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok


def test_file_rename(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    sha = repo.commit("rename", rename={"web/util.js": "web/helpers.js"})
    env.scan(repo_id)
    record = env.records(repo_id)[-1]
    (renamed,) = [f for f in record["files"]["items"] if f["status"] == "R"]
    assert (renamed["path"], renamed["old_path"]) == ("web/helpers.js", "web/util.js")
    assert record["graph_delta"]["added_symbols"] == ["web/helpers.js:fmt()"]
    assert record["graph_delta"]["removed_symbols"] == ["web/util.js:fmt()"]
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok


def test_one_commit_with_add_edit_and_delete_together(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    sha = repo.commit(
        "mixed",
        {"app/new.py": "def n():\n    return 1\n", "app/orders.py": ORDERS_EDIT},
        delete=["web/util.js"],
    )
    env.scan(repo_id)
    record = env.records(repo_id)[-1]
    assert record["files"]["counts"] == {"A": 1, "M": 1, "D": 1}
    assert verify_snapshot(GitAdapter(repo.path), env.current_dir(repo_id), sha).ok
    assert count(env.engine, "SELECT count(*) FROM change_records") == 2


ORDERS_EDIT = "from app.pricing import average_item_price\n\n\nclass Orders:\n    def avg(self, items):\n        return average_item_price(2, len(items))\n\n    def total(self, items):\n        return len(items)\n"


def test_scanning_a_later_commit_covers_every_intermediate_commit(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    c1 = repo.commit("one", {"a1.py": "x = 1\n"})
    c2 = repo.commit("two", {"a2.py": "y = 2\n"})
    c3 = repo.commit("three", {"a1.py": "x = 3\n"})
    env.scan(repo_id)  # default branch tip, skipping c1 and c2 as separate scans
    record = env.records(repo_id)[-1]
    assert record["commits"] == [c3, c2, c1]  # newest first, all real commits
    assert record["files"]["counts"] == {"A": 2}
    assert count(env.engine, "SELECT count(*) FROM repository_commits") >= 4


def test_scanning_a_specific_older_revision(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    first = repo.git("rev-parse", "HEAD")
    repo.commit("later", {"later.py": "z = 1\n"})
    env.scan(repo_id, revision=first)
    assert env.repo(repo_id)["indexed_sha"] == first
    env.scan(repo_id)  # now the tip
    assert env.records(repo_id)[-1]["base_sha"] == first


def test_change_records_and_history_carry_no_author_information(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    scan, _ = env.scan(repo_id)
    dump = json.dumps(env.records(repo_id)) + json.dumps(
        env.scans.scan_history(PROJECT, scan["scan_id"])
    )
    dump += json.dumps(env.scans.get_scan(PROJECT, scan["scan_id"])) + json.dumps(env.repo(repo_id))
    with env.engine.connect() as conn:
        import sqlalchemy as sa

        dump += " ".join(str(r) for r in conn.execute(sa.text("SELECT * FROM repository_commits")))
    assert AUTHOR_NAME not in dump and AUTHOR_EMAIL not in dump and "hostile" not in dump.lower()


PRICING_EDIT = "def average_item_price(total, qty):\n    return total // max(qty, 1)\n\n\ndef regression():\n    return average_item_price(0, 0)\n"


def test_commit_subjects_are_redacted_before_storage(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    repo.commit("fix login password=hunter2-correct-horse", {"x.py": "a = 1\n"})
    env.scan(repo_id)
    with env.engine.connect() as conn:
        import sqlalchemy as sa

        subjects = [r[0] for r in conn.execute(sa.text("SELECT subject FROM repository_commits"))]
    assert subjects and not any("hunter2" in s for s in subjects)
    assert any("[REDACTED:" in s for s in subjects)


# ---- read-only boundary (M2-8) -----------------------------------------------------------


def test_the_monitored_repository_is_byte_identical_after_register_scan_update(env) -> None:
    repo = make_repo(env)
    before = tree_fingerprint(repo.path)
    repo_id = env.register(repo.path)
    env.scan(repo_id)
    assert tree_fingerprint(repo.path) == before
    repo.commit("edit", {"app/pricing.py": PRICING_EDIT})
    after_commit = tree_fingerprint(repo.path)
    env.scan(repo_id)
    env.service.published_graph(PROJECT, repo_id)
    assert tree_fingerprint(repo.path) == after_commit
    assert not (repo.path / "graphify-out").exists()  # Graphify never ran inside the repository


def test_all_writes_land_in_the_data_directory(env, tmp_path) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    outside_before = {
        k for k in tree_fingerprint(tmp_path) if not k.startswith(("data/", "repos/"))
    }
    env.scan(repo_id)
    outside_after = {k for k in tree_fingerprint(tmp_path) if not k.startswith(("data/", "repos/"))}
    assert outside_after == outside_before
    root = env.data_dir / "repos" / repo_id
    assert {p.name for p in root.iterdir()} <= {"current", "current.sha", "graphs", "work", "trash"}
    env.assert_clean(repo_id)  # work/ and trash/ exist but are empty


# ---- enqueue validation ------------------------------------------------------------------


def test_enqueue_rejects_unknown_repositories_and_hostile_or_missing_revisions(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    with pytest.raises(RepoError) as info:
        env.service.enqueue(PROJECT, "no-such-repository")
    assert info.value.code == "not_found"
    with pytest.raises(RepoError) as info:
        env.service.enqueue("project-other", repo_id)  # another project's repository
    assert info.value.code == "not_found"
    for bad in ("--upload-pack=x", "HEAD~1", "main..HEAD", "a b", "$(id)"):
        with pytest.raises(RepoError) as info:
            env.service.enqueue(PROJECT, repo_id, bad)
        assert info.value.code == "invalid_revision"
    with pytest.raises(RepoError) as info:
        env.service.enqueue(PROJECT, repo_id, "no-such-branch")
    assert info.value.code == "not_found"
    assert count(env.engine, "SELECT count(*) FROM repository_scans") == 0


def test_a_scan_cannot_be_read_through_another_projects_scope(env) -> None:
    repo = make_repo(env)
    repo_id = env.register(repo.path)
    scan, _ = env.scan(repo_id)
    assert env.scans.get_scan("project-other", scan["scan_id"]) is None
    assert env.scans.scan_history("project-other", scan["scan_id"]) == []
    assert env.scans.list_change_records("project-other", repo_id, limit=10, cursor=None)[0] == []
    assert env.service.published_graph("project-other", repo_id) is None
