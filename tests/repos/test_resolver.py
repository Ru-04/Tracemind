"""M2-7: release-to-commit mappings stay explicitly unknown unless a telemetry-supplied, exact,
full commit id exists in the registered repository. Nothing is inferred."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

from pathlib import Path

import pytest
from tracemind.repos.git_adapter import GitAdapter
from tracemind.repos.resolver import resolve_release_commit

from tests.helpers_git import TempRepo


@pytest.fixture
def repo(tmp_path: Path) -> TempRepo:
    r = TempRepo(tmp_path / "app")
    r.commit("one", {"a.py": "1\n"})
    r.git("tag", "v1.2.0")
    r.commit("two", {"a.py": "2\n"})
    return r


@pytest.fixture
def git(repo: TempRepo) -> GitAdapter:
    return GitAdapter(repo.path)


def test_a_full_sha_that_exists_is_verified_and_says_where_the_claim_came_from(repo, git) -> None:
    sha = repo.git("rev-parse", "HEAD")
    res = resolve_release_commit(git, release="1.2.0", commit_sha=sha)
    assert res.to_json() == {
        "commit_sha": sha,
        "confidence": "verified",
        "source": "telemetry.commit_sha",
        "reason": None,
    }


def test_an_older_commit_that_exists_is_also_verified(repo, git) -> None:
    first = repo.git("rev-parse", "HEAD~1")
    assert (
        resolve_release_commit(git, commit_sha=first.upper()).commit_sha == first
    )  # case-insensitive


@pytest.mark.parametrize(
    ("commit_sha", "reason"),
    [
        (None, "no_commit_in_telemetry"),
        ("", "no_commit_in_telemetry"),
        ("0" * 40, "commit_not_in_repository"),
        ("f" * 40, "commit_not_in_repository"),
        ("abcdef1", "abbreviated_sha"),
        ("a" * 39, "abbreviated_sha"),
        ("HEAD", "invalid_commit_sha"),
        ("main", "invalid_commit_sha"),
        ("v1.2.0", "invalid_commit_sha"),
        ("HEAD~1", "invalid_commit_sha"),
        ("--upload-pack=x", "invalid_commit_sha"),
        ("g" * 40, "invalid_commit_sha"),
        ("a" * 41, "invalid_commit_sha"),
        (12345, "invalid_commit_sha"),
        (["a" * 40], "invalid_commit_sha"),
    ],
)
def test_everything_else_is_unknown_with_a_reason(commit_sha, reason, git) -> None:
    res = resolve_release_commit(git, release="1.2.0", commit_sha=commit_sha)
    assert (res.commit_sha, res.confidence, res.source, res.reason) == (
        None,
        "unknown",
        None,
        reason,
    )


def test_no_registered_repository_is_unknown(repo) -> None:
    sha = repo.git("rev-parse", "HEAD")
    res = resolve_release_commit(None, release="1.2.0", commit_sha=sha)
    assert (res.confidence, res.reason) == ("unknown", "no_repository")


def test_a_release_name_is_never_turned_into_a_commit(repo, git) -> None:
    """The repository has a tag called v1.2.0 and a branch called main; neither is used."""
    for release in ("v1.2.0", "1.2.0", "main", "HEAD", repo.git("rev-parse", "HEAD")):
        res = resolve_release_commit(git, release=release, commit_sha=None)
        assert (res.commit_sha, res.confidence, res.reason) == (
            None,
            "unknown",
            "no_commit_in_telemetry",
        )


def test_timestamps_and_proximity_play_no_role(repo, git) -> None:
    """A commit made 'right at' the incident time is not matched to anything."""
    res = resolve_release_commit(git, release=None, commit_sha=None)
    assert res.confidence == "unknown"
    assert not hasattr(res, "estimated") and res.confidence != "estimated"


def test_rejected_input_never_reaches_git(repo, monkeypatch) -> None:
    g = GitAdapter(repo.path)
    calls: list[object] = []

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("git was invoked")

    monkeypatch.setattr(GitAdapter, "_exec", spy)
    for bad in (None, "", "abc", "HEAD", "--x", "main", 7, "g" * 40):
        resolve_release_commit(g, commit_sha=bad)
    assert calls == []


def test_a_repository_error_is_reported_as_unknown_not_raised(tmp_path: Path) -> None:
    gone = GitAdapter(tmp_path / "does-not-exist")
    res = resolve_release_commit(gone, commit_sha="a" * 40)
    assert (res.confidence, res.reason) == ("unknown", "repository_unavailable")
