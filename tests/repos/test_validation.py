"""M2-6 (validation part): hostile revisions, paths and repository locations are rejected before
any git call or filesystem access, with stable codes that never echo the input."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

from pathlib import Path

import pytest
from tracemind.repos.errors import RepoError
from tracemind.repos.validate import (
    is_safe_snapshot_path,
    resolve_repository_path,
    validate_full_sha,
    validate_repo_path,
    validate_revision,
)

from tests.helpers_git import TempRepo, make_dir_link

HOSTILE_REVISIONS = [
    "--upload-pack=touch /tmp/pwned",
    "--output=/etc/passwd",
    "-p",
    "-",
    "HEAD~1",
    "HEAD^",
    "HEAD^{tree}",
    "main..feature",
    "main...feature",
    "HEAD@{1}",
    "@{u}",
    "main:README.md",
    "main; rm -rf /",
    "main && whoami",
    "main|cat",
    "main`id`",
    "$(id)",
    "a b",
    "main\n--exec=x",
    "main\x00x",
    "\\\\server\\share",
    "C:\\Windows",
    "feature/.hidden",
    ".hidden",
    "trailing/",
    "branch.lock",
    "ends.",
    "dou//ble",
    "*",
    "ma?n",
    "ma[in]",
    "x" * 256,
    "",
    None,
    123,
    ["main"],
    b"main",
]


@pytest.mark.parametrize("value", HOSTILE_REVISIONS, ids=lambda v: repr(v)[:30])
def test_hostile_revisions_are_rejected(value) -> None:
    with pytest.raises(RepoError) as info:
        validate_revision(value)
    assert info.value.code == "invalid_revision"
    assert str(value)[:10] not in str(info.value) or len(str(value)) < 3  # never echoes input


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("main", "main"),
        ("feature/login-fix", "feature/login-fix"),
        ("v1.2.3", "v1.2.3"),
        ("HEAD", "HEAD"),
        ("ABCDEF1", "abcdef1"),
        ("0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"),
    ],
)
def test_ordinary_revisions_are_accepted(value, expected) -> None:
    assert validate_revision(value) == expected


@pytest.mark.parametrize(
    "value", ["abc", "g" * 40, "A" * 41, "", None, "0123456789abcdef0123456789abcdef0123456"]
)
def test_full_sha_requires_exactly_forty_lowercase_hex(value) -> None:
    with pytest.raises(RepoError):
        validate_full_sha(value)


HOSTILE_PATHS = [
    "../etc/passwd",
    "a/../../b",
    "a/./b",
    "a//b",
    "/abs/path",
    "C:/Windows/system32",
    "C:\\Windows",
    "\\\\server\\share\\x",
    "a\\b",
    "dir/",
    ".git/config",
    "sub/.git/hooks/pre-commit",
    "sub/.GIT/x",
    "-rf",
    "--option",
    "file:stream",
    "file\x00.py",
    "file\n.py",
    "a" * 1025,
    "dir/" + "b" * 256,
    "",
    ".",
    "..",
    None,
    5,
]


@pytest.mark.parametrize("value", HOSTILE_PATHS, ids=lambda v: repr(v)[:30])
def test_hostile_paths_are_rejected(value) -> None:
    with pytest.raises(RepoError) as info:
        validate_repo_path(value)
    assert info.value.code == "invalid_path"


@pytest.mark.parametrize(
    "value",
    [
        "app/pricing.py",
        "a.b/c-d/e_f.txt",
        "docs/README.md",
        "web/[id].js",
        "x y/z.py",
        "ünï/code.py",
    ],
)
def test_ordinary_paths_are_accepted(value) -> None:
    assert validate_repo_path(value) == value


@pytest.mark.parametrize(
    ("value", "safe"),
    [
        ("app/main.py", True),
        ("CON", False),
        ("dir/nul.txt", False),
        ("dir/COM1.py", False),
        ("lpt9", False),
        ("trailing.", False),
        ("dir/trailing ", False),
        ("a/b/c.py", True),
        ("../x", False),
        ("a:b", False),
    ],
)
def test_snapshot_paths_also_refuse_windows_hostile_names(value, safe) -> None:
    assert is_safe_snapshot_path(value) is safe


# ---- repository locations ----------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "repos"
    r.mkdir()
    return r


def code(excinfo) -> tuple[str, str | None]:
    return excinfo.value.code, excinfo.value.detail


def test_repository_inside_allowed_root_is_accepted(root: Path) -> None:
    repo = TempRepo(root / "app")
    assert resolve_repository_path(str(repo.path), [root]) == repo.path.resolve()


def test_path_traversal_out_of_the_root_is_rejected(root: Path, tmp_path: Path) -> None:
    TempRepo(tmp_path / "outside")
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(root / ".." / "outside"), [root])
    assert code(info) == ("invalid_repository_path", "outside_allowed_roots")


def test_sibling_with_a_shared_name_prefix_is_not_inside_the_root(
    root: Path, tmp_path: Path
) -> None:
    TempRepo(tmp_path / "repos-evil")
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(tmp_path / "repos-evil"), [root])
    assert info.value.detail == "outside_allowed_roots"


def test_symlink_or_junction_escape_is_rejected(root: Path, tmp_path: Path) -> None:
    TempRepo(tmp_path / "secret-repo")
    link = root / "innocent"
    if not make_dir_link(link, tmp_path / "secret-repo"):
        pytest.skip("cannot create a symlink or junction on this machine")
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(link), [root])
    assert info.value.detail == "outside_allowed_roots"


def test_git_entry_pointing_outside_the_root_is_rejected(root: Path, tmp_path: Path) -> None:
    outside = TempRepo(tmp_path / "real")
    disguised = root / "disguised"
    disguised.mkdir()
    (disguised / ".git").write_text(f"gitdir: {outside.path / '.git'}\n", encoding="utf-8")
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(disguised), [root])
    assert info.value.detail == "git_dir_outside_allowed_roots"


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        ("", "empty_or_not_text"),
        (None, "empty_or_not_text"),
        ("relative/path", "not_absolute"),
        ("x\x00y", "forbidden_characters"),
    ],
)
def test_malformed_repository_paths(raw, detail, root: Path) -> None:
    with pytest.raises(RepoError) as info:
        resolve_repository_path(raw, [root])
    assert info.value.detail == detail


def test_missing_non_git_and_unconfigured_roots(root: Path) -> None:
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(root / "nope"), [root])
    assert info.value.detail == "not_found"
    plain = root / "plain"
    plain.mkdir()
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(plain), [root])
    assert info.value.detail == "not_a_git_repository"
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(plain), [])
    assert info.value.detail == "no_allowed_roots_configured"


def test_a_file_is_not_a_repository(root: Path) -> None:
    f = root / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(f), [root])
    assert info.value.detail == "not_a_directory"


def test_forbidden_locations_such_as_the_data_directory(root: Path) -> None:
    repo = TempRepo(root / "app")
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(repo.path), [root], forbidden=[root])
    assert info.value.detail == "forbidden_location"
    with pytest.raises(RepoError) as info:
        resolve_repository_path(str(repo.path), [root], forbidden=[repo.path / "data"])
    assert info.value.detail == "forbidden_location"  # data dir inside the repo is refused too
