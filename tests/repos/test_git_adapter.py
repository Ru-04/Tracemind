"""M2-3 (real Git history and diffs) and the adapter side of M2-6 / M2-8, using real temporary
repositories."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import GitAdapter

from tests.helpers_git import AUTHOR_EMAIL, AUTHOR_NAME, TempRepo, tree_fingerprint


@pytest.fixture
def repo(tmp_path: Path) -> TempRepo:
    r = TempRepo(tmp_path / "app")
    r.commit(
        "initial", {"app/pricing.py": "def f(a, b):\n    return a // b\n", "README.md": "hi\n"}
    )
    return r


@pytest.fixture
def git(repo: TempRepo) -> GitAdapter:
    g = GitAdapter(repo.path)
    g.verify_repository()
    return g


def test_identity_head_branch_and_resolution(repo, git) -> None:
    root = repo.git("rev-list", "--max-parents=0", "HEAD")
    head = repo.git("rev-parse", "HEAD")
    assert git.identity() == f"root:{root}"
    assert git.head_sha() == head
    assert git.default_branch() == "main"
    assert git.resolve_commit("HEAD") == head
    assert git.resolve_commit(head[:8]) == head
    assert git.resolve_commit("main") == head
    assert git.commit_exists(head) and not git.commit_exists("0" * 40)


def test_missing_revisions_are_clean_not_found_errors(git) -> None:
    for rev in ("nonexistent-branch", "0" * 40, "deadbeef"):
        with pytest.raises(RepoError) as info:
            git.resolve_commit(rev)
        assert info.value.code == "not_found"


def test_empty_repository_is_reported(tmp_path: Path) -> None:
    empty = TempRepo(tmp_path / "empty")
    g = GitAdapter(empty.path)
    g.verify_repository()
    with pytest.raises(RepoError) as info:
        g.head_sha()
    assert info.value.code == "empty_repository"
    with pytest.raises(RepoError):
        g.identity()


def test_not_a_repository_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepoError) as info:
        GitAdapter(plain).verify_repository()
    assert info.value.code == "invalid_repository_path"


def test_log_returns_real_commits_newest_first_without_author_data(repo, git) -> None:
    c2 = repo.commit("add audit", {"app/audit.py": "x = 1\n"})
    c3 = repo.commit("tweak password=hunter2-correct-horse", {"app/audit.py": "x = 2\n"})
    log = git.log("HEAD")
    assert [c.sha for c in log] == [c3, c2, repo.git("rev-list", "--max-parents=0", "HEAD")]
    assert log[0].parents == (c2,) and log[2].parents == ()
    assert log[0].committed_at.isoformat().endswith("+00:00")
    assert "hunter2" not in log[0].subject and "[REDACTED:" in log[0].subject
    dumped = " ".join(repr(c) for c in log)
    assert (
        AUTHOR_NAME not in dumped and AUTHOR_EMAIL not in dumped and "hostile" not in dumped.lower()
    )
    assert not any(
        hasattr(c, a) for c in log for a in ("author", "author_name", "author_email", "email")
    )
    assert len(git.log("HEAD", limit=1)) == 1
    assert git.commit(c2).sha == c2


def test_subject_is_truncated(repo, git) -> None:
    repo.commit("s" * 500, {"a.txt": "a"})
    assert len(git.log("HEAD", limit=1)[0].subject) == 200


def test_diff_covers_add_modify_delete_rename_and_binary(repo, git) -> None:
    base = repo.git("rev-parse", "HEAD")
    target = repo.commit(
        "mixed",
        files={
            "app/pricing.py": "def f(a, b):\n    if b == 0:\n        return 0\n    return a // b\n",
            "app/new.py": "n = 1\n",
            "blob.bin": b"\x00\x01\x02\xff" * 10,
        },
        delete=["README.md"],
    )
    by_path = {c.path: c for c in git.diff_files(base, target)}
    assert by_path["app/new.py"].status == "A" and by_path["app/new.py"].additions == 1
    assert by_path["app/pricing.py"].status == "M"
    assert (by_path["app/pricing.py"].additions, by_path["app/pricing.py"].deletions) == (2, 0)
    assert by_path["README.md"].status == "D" and by_path["README.md"].oid is None
    assert by_path["blob.bin"].binary and by_path["blob.bin"].additions is None
    assert all(c.oid and c.mode for c in by_path.values() if c.status != "D")

    renamed = repo.commit("rename", rename={"app/new.py": "app/renamed.py"})
    rn = {c.path: c for c in git.diff_files(target, renamed)}
    assert rn["app/renamed.py"].status == "R" and rn["app/renamed.py"].old_path == "app/new.py"
    assert "app/new.py" not in rn


def test_diff_from_the_empty_tree_lists_every_file(repo, git) -> None:
    changes = git.diff_files(None, "HEAD")
    assert {(c.path, c.status) for c in changes} == {("README.md", "A"), ("app/pricing.py", "A")}


def test_file_diff_is_real_and_bounded(repo, git) -> None:
    base = repo.git("rev-parse", "HEAD")
    target = repo.commit("edit", {"app/pricing.py": "def f(a, b):\n    return a // (b or 1)\n"})
    fd = git.file_diff(base, target, "app/pricing.py")
    assert "-    return a // b" in fd.patch and "+    return a // (b or 1)" in fd.patch
    assert not fd.truncated and not fd.binary
    small = git.file_diff(base, target, "app/pricing.py", max_bytes=40)
    assert small.truncated and len(small.patch.encode()) <= 40
    bigfile = repo.commit("big", {"big.txt": "line\n" * 20000})
    assert git.file_diff(target, bigfile, "big.txt", max_bytes=1000).truncated
    binary = repo.commit("bin", {"b.bin": b"\x00\xff" * 50})
    assert git.file_diff(bigfile, binary, "b.bin").binary


def test_file_diff_refuses_unknown_and_hostile_paths(repo, git) -> None:
    with pytest.raises(RepoError) as info:
        git.file_diff(None, "HEAD", "does/not/exist.py")
    assert info.value.code == "not_found"
    for bad in ("../x", "/etc/passwd", ".git/config", "-p"):
        with pytest.raises(RepoError) as info:
            git.file_diff(None, "HEAD", bad)
        assert info.value.code == "invalid_path"


def test_tree_entry_matches_exactly_never_by_prefix_or_pattern(repo, git) -> None:
    assert git.tree_entry("HEAD", "app/pricing.py") is not None
    assert git.tree_entry("HEAD", "app") is None  # a directory, not a file
    assert git.tree_entry("HEAD", "app/pric") is None  # prefix
    assert git.tree_entry("HEAD", "app/*.py") is None  # pattern: literal pathspecs only
    assert git.tree_entry("HEAD", "nope.py") is None


def test_merge_commits_and_multiple_parents(repo, git) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    f = repo.commit("feature work", {"feature.txt": "f\n"})
    repo.git("checkout", "-q", "main")
    m = repo.commit("main work", {"main.txt": "m\n"})
    repo.git("merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merged = repo.git("rev-parse", "HEAD")
    info = git.commit(merged)
    assert set(info.parents) == {f, m}
    assert {c.path for c in git.diff_files(m, merged)} == {"feature.txt"}


# ---- M2-6: git is never invoked for rejected input ---------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.resolve_commit("--upload-pack=x"),
        lambda g: g.resolve_commit("HEAD~1"),
        lambda g: g.log("main..HEAD"),
        lambda g: g.commit("a b"),
        lambda g: g.diff_files("-p", "HEAD"),
        lambda g: g.diff_files(None, "$(id)"),
        lambda g: g.file_diff(None, "HEAD", "../secret"),
        lambda g: g.tree_entry("HEAD", ".git/config"),
        lambda g: g.ls_tree("HEAD:file"),
    ],
)
def test_rejected_input_never_reaches_git(call, repo, monkeypatch) -> None:
    g = GitAdapter(repo.path)
    calls: list[object] = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("git was invoked for rejected input")

    monkeypatch.setattr(GitAdapter, "_exec", spy)
    with pytest.raises(RepoError):
        call(g)
    assert calls == []


# ---- limits, environment, read-only ------------------------------------------------------


def test_timeouts_are_enforced_and_reported(repo) -> None:
    slow = GitAdapter(
        repo.path, git_cmd=[sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3
    )
    with pytest.raises(RepoError) as info:
        slow.head_sha()
    assert info.value.code == "timeout"


def test_output_cap_is_enforced(repo) -> None:
    repo.commit("many", {f"f{i}.txt": "x" for i in range(200)})
    g = GitAdapter(repo.path, max_output=500)
    with pytest.raises(RepoError) as info:
        g.ls_tree("HEAD")
    assert info.value.code == "output_too_large"


def test_child_environment_is_minimal_and_has_no_credentials(repo, monkeypatch) -> None:
    for name, value in {
        "ANTHROPIC_API_KEY": "k1",
        "GROQ_API_KEY": "k2",
        "GITHUB_TOKEN": "k3",
        "SNOWFLAKE_PASSWORD": "k4",
        "TRACEMIND_KEY_PROJECT_DEMO": "k5",
        "HTTPS_PROXY": "http://user:pw@proxy",
        "GIT_DIR": "/elsewhere",
        "GIT_EXTERNAL_DIFF": "evil",
    }.items():
        monkeypatch.setenv(name, value)
    env = GitAdapter(repo.path)._env
    assert not {
        k for k in env if k.startswith(("ANTHROPIC", "GROQ", "GITHUB", "SNOWFLAKE", "TRACEMIND"))
    }
    assert "HTTPS_PROXY" not in env and "GIT_DIR" not in env and "GIT_EXTERNAL_DIFF" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_LITERAL_PATHSPECS"] == "1"
    assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_repository_config_cannot_run_external_programs(repo, git, tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    script = tmp_path / "evil.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    cmd = f'"{sys.executable}" "{script}"'.replace("\\", "/")
    repo.git("config", "diff.external", cmd)
    repo.git("config", "diff.evil.textconv", cmd)
    repo.write(".gitattributes", "*.py diff=evil\n")
    base = repo.git("rev-parse", "HEAD")
    target = repo.commit(
        "edit", {"app/pricing.py": "changed = True\n", ".gitattributes": "*.py diff=evil\n"}
    )
    git.diff_files(base, target)
    git.file_diff(base, target, "app/pricing.py")
    assert not marker.exists()


def test_read_only_operations_leave_the_repository_byte_identical(
    repo, git, tmp_path: Path
) -> None:
    repo.commit("second", {"app/audit.py": "x = 1\n"}, delete=["README.md"])
    before = tree_fingerprint(repo.path)
    head = git.head_sha()
    git.identity(), git.default_branch(), git.log("HEAD"), git.commit(head)
    git.diff_files(None, head)
    git.file_diff(None, head, "app/audit.py")
    git.ls_tree(head)
    git.tree_entry(head, "app/audit.py")
    with git.blob_reader() as reader:
        entry = git.tree_entry(head, "app/audit.py")
        assert entry is not None
        assert reader.read(entry.oid, max_bytes=1000) == b"x = 1\n"
    assert tree_fingerprint(repo.path) == before


def test_no_author_flags_are_ever_requested() -> None:
    import inspect

    from tracemind.repos import git_adapter

    source = inspect.getsource(git_adapter)
    for forbidden in ("%an", "%ae", "%aE", "%aN", "%cn", "%ce", "%cN", "%cE", "--author"):
        assert forbidden not in source
