"""Snapshot export, incremental apply and verification (M2-4 groundwork, M2-6 and M2-8 parts)."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import os
from pathlib import Path

import pytest
from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import GitAdapter, TreeEntry
from tracemind.repos.snapshot import (
    SnapshotLimits,
    apply_changes,
    classify_entry,
    export_snapshot,
    plan_export,
    verify_snapshot,
)

from tests.helpers_git import TempRepo, tree_fingerprint


@pytest.fixture
def repo(tmp_path: Path) -> TempRepo:
    r = TempRepo(tmp_path / "monitored")
    r.commit(
        "initial",
        {
            "app/pricing.py": "def f(a, b):\n    return a // b\n",
            "app/orders.py": "from app.pricing import f\n",
            "web/util.js": "export const x = 1;\n",
            "README.md": "hello\n",
        },
    )
    return r


@pytest.fixture
def git(repo: TempRepo) -> GitAdapter:
    return GitAdapter(repo.path)


def files_in(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "graphify-out" not in p.parts
    }


def test_export_materializes_exactly_the_commit(repo, git, tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    report = export_snapshot(git, "HEAD", dest)
    assert report.commit_sha == repo.git("rev-parse", "HEAD")
    assert report.files_written == 4 and report.skipped == {}
    assert files_in(dest) == {"app/pricing.py", "app/orders.py", "web/util.js", "README.md"}
    assert (dest / "app/pricing.py").read_text(
        encoding="utf-8"
    ) == "def f(a, b):\n    return a // b\n"
    assert verify_snapshot(git, dest, "HEAD").ok


def test_snapshot_is_the_commit_not_the_working_tree(repo, git, tmp_path: Path) -> None:
    sha = repo.git("rev-parse", "HEAD")
    (repo.path / "app/pricing.py").write_text("UNCOMMITTED EDIT\n", encoding="utf-8")
    (repo.path / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    dest = tmp_path / "snap"
    export_snapshot(git, sha, dest)
    assert "UNCOMMITTED" not in (dest / "app/pricing.py").read_text(encoding="utf-8")
    assert not (dest / "untracked.py").exists()


def test_export_is_read_only_for_the_monitored_repository(repo, git, tmp_path: Path) -> None:
    before = tree_fingerprint(repo.path)
    export_snapshot(git, "HEAD", tmp_path / "snap")
    assert tree_fingerprint(repo.path) == before


def test_special_entries_are_skipped_and_counted(repo, git, tmp_path: Path) -> None:
    repo.add_symlink_entry("link-to-secret", "../../etc/passwd")
    repo.add_submodule_entry("vendor/sub", repo.git("rev-parse", "HEAD"))
    repo.write("big.txt", "x" * 3000)
    repo.git("add", "big.txt")  # not `add -A`: that would drop the index-only symlink entries
    repo.git("commit", "-q", "-m", "specials")
    dest = tmp_path / "snap"
    report = export_snapshot(git, "HEAD", dest, SnapshotLimits(max_file_bytes=2000))
    assert report.skipped == {"symlink": 1, "submodule": 1, "too_large": 1}
    assert "link-to-secret" not in files_in(dest) and "big.txt" not in files_in(dest)
    assert not (dest / "link-to-secret").exists() and not (dest / "vendor").exists()
    assert verify_snapshot(git, dest, "HEAD", SnapshotLimits(max_file_bytes=2000)).ok


def E(path: str, mode: str = "100644", typ: str = "blob", size: int = 1) -> TreeEntry:
    return TreeEntry(mode, typ, "a" * 40, size, path)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (E("ok/file.py"), None),
        (E("link", mode="120000"), "symlink"),
        (E("sub", mode="160000", typ="commit"), "submodule"),
        (E("x", mode="040000", typ="tree"), "unsupported_mode"),
        (E("../escape.py"), "unsafe_path"),
        (E("a/../../escape.py"), "unsafe_path"),
        (E("/abs.py"), "unsafe_path"),
        (E("C:/Windows/x.py"), "unsafe_path"),
        (E("a\\b.py"), "unsafe_path"),
        (E(".git/hooks/pre-commit"), "unsafe_path"),
        (E("sub/.GIT/config"), "unsafe_path"),
        (E("nul.txt"), "unsafe_path"),
        (E("dir/CON"), "unsafe_path"),
        (E("file.txt:stream"), "unsafe_path"),
        (E("trailing."), "unsafe_path"),
        (E("graphify-out/graph.json"), "unsafe_path"),
        (E("bad\udcffname.py"), "non_utf8_path"),
        (E("huge.bin", size=10**9), "too_large"),
    ],
)
def test_classification_of_hostile_tree_entries(entry, reason) -> None:
    assert classify_entry(entry, SnapshotLimits(), set()) == reason


def test_case_colliding_names_keep_only_the_first() -> None:
    included, skipped = plan_export([E("A.py"), E("a.py"), E("b.py")], SnapshotLimits())
    assert [e.path for e in included] == ["A.py", "b.py"] and skipped == {"case_collision": 1}


def test_global_limits_fail_the_export_instead_of_truncating() -> None:
    many = [E(f"f{i}.py") for i in range(5)]
    with pytest.raises(RepoError) as info:
        plan_export(many, SnapshotLimits(max_files=3))
    assert (info.value.code, info.value.detail) == ("snapshot_limit_exceeded", "too_many_files")
    with pytest.raises(RepoError) as info:
        plan_export([E("a.py", size=60), E("b.py", size=60)], SnapshotLimits(max_total_bytes=100))
    assert info.value.detail == "total_size"


def test_failed_export_leaves_no_partial_snapshot(repo, git, tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "snap"
    real = GitAdapter.blob_reader
    calls = {"n": 0}

    class Flaky:
        def __init__(self, inner) -> None:
            self.inner = inner

        def read(self, oid: str, *, max_bytes: int) -> bytes:
            calls["n"] += 1
            if calls["n"] == 3:
                raise RepoError("timeout")
            data: bytes = self.inner.read(oid, max_bytes=max_bytes)
            return data

    from contextlib import contextmanager

    @contextmanager
    def flaky(self, **kw):
        with real(self, **kw) as reader:
            yield Flaky(reader)

    monkeypatch.setattr(GitAdapter, "blob_reader", flaky)
    with pytest.raises(RepoError) as info:
        export_snapshot(git, "HEAD", dest)
    assert info.value.code == "timeout"
    assert not dest.exists()
    assert [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".snap")
    ] == []  # temp dir removed


def test_export_refuses_to_overwrite_an_existing_directory(repo, git, tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(RepoError):
        export_snapshot(git, "HEAD", dest)
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_apply_changes_handles_add_edit_delete_and_rename(repo, git, tmp_path: Path) -> None:
    base = repo.git("rev-parse", "HEAD")
    dest = tmp_path / "snap"
    export_snapshot(git, base, dest)
    target = repo.commit(
        "mixed",
        files={
            "app/pricing.py": "def f(a, b):\n    return 0 if not b else a // b\n",
            "app/audit.py": "from app.pricing import f\n",
        },
        delete=["README.md"],
        rename={"web/util.js": "web/helpers.js"},
    )
    changes = git.diff_files(base, target)
    assert {(c.path, c.status) for c in changes} == {
        ("app/pricing.py", "M"),
        ("app/audit.py", "A"),
        ("README.md", "D"),
        ("web/helpers.js", "R"),
    }
    apply_changes(git, dest, changes)
    assert files_in(dest) == {"app/pricing.py", "app/orders.py", "app/audit.py", "web/helpers.js"}
    assert verify_snapshot(git, dest, target).ok
    assert not (dest / "web").joinpath("util.js").exists()


def test_apply_removes_emptied_directories(repo, git, tmp_path: Path) -> None:
    base = repo.git("rev-parse", "HEAD")
    dest = tmp_path / "snap"
    export_snapshot(git, base, dest)
    target = repo.commit("drop web", delete=["web/util.js"])
    apply_changes(git, dest, git.diff_files(base, target))
    assert not (dest / "web").exists()
    assert verify_snapshot(git, dest, target).ok


def test_apply_replaces_a_file_with_a_symlink_by_removing_it(repo, git, tmp_path: Path) -> None:
    base = repo.git("rev-parse", "HEAD")
    dest = tmp_path / "snap"
    export_snapshot(git, base, dest)
    repo.git("rm", "-q", "--cached", "README.md")
    repo.add_symlink_entry("README.md", "app/pricing.py")
    repo.git("commit", "-q", "-m", "readme becomes a symlink")
    target = repo.git("rev-parse", "HEAD")
    report = apply_changes(git, dest, git.diff_files(base, target))
    assert report.skipped == {"symlink": 1}
    assert not (dest / "README.md").exists()
    assert verify_snapshot(git, dest, target).ok


def test_verify_detects_missing_extra_and_tampered_files(repo, git, tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    export_snapshot(git, "HEAD", dest)
    (dest / "README.md").unlink()
    (dest / "app/pricing.py").write_text("tampered\n", encoding="utf-8")
    (dest / "extra.py").write_text("x\n", encoding="utf-8")
    result = verify_snapshot(git, dest, "HEAD")
    assert not result.ok
    assert result.missing == ["README.md"]
    assert result.mismatched == ["app/pricing.py"]
    assert result.extra == ["extra.py"]


def test_verify_ignores_graphify_output_directory(repo, git, tmp_path: Path) -> None:
    dest = tmp_path / "snap"
    export_snapshot(git, "HEAD", dest)
    (dest / "graphify-out").mkdir()
    (dest / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
    assert verify_snapshot(git, dest, "HEAD").ok


def test_nothing_is_ever_written_outside_the_snapshot_directory(repo, git, tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    before = tree_fingerprint(tmp_path)
    dest = sandbox / "snap"
    export_snapshot(git, "HEAD", dest)
    after = tree_fingerprint(tmp_path)
    new = {k for k in after if k not in before}
    assert new and all(k.startswith("sandbox/snap/") for k in new)
    changed = {k for k in before if after.get(k) != before[k]}
    assert changed == set()
    assert os.path.exists(dest)
