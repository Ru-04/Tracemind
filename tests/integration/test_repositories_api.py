"""S2: repository registration and listing — allowed roots, hostile paths, project isolation, and
the append-only guarantees of the M2 tables."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from tracemind.api.app import create_app
from tracemind.config import Settings
from tracemind.projects import ConfigProjectRegistry

from tests.conftest import NOW
from tests.helpers_git import AUTHOR_EMAIL, AUTHOR_NAME, TempRepo, make_dir_link, tree_fingerprint

pytestmark = pytest.mark.integration


@pytest.fixture
def keys() -> dict[str, str]:
    return {"alpha": secrets.token_urlsafe(32), "beta": secrets.token_urlsafe(32)}


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repos"
    root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return root, data


@pytest.fixture
def client(store, tmp_path: Path, keys: dict[str, str], roots) -> TestClient:
    root, data = roots
    cfg = {
        "projects": [
            {"project_id": "project-demo", "allowed_services": ["s"], "ingest_key_env": "KA"},
            {"project_id": "project-other", "allowed_services": ["s"], "ingest_key_env": "KB"},
        ]
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    registry = ConfigProjectRegistry.from_file(path, {"KA": keys["alpha"], "KB": keys["beta"]})
    app = create_app(
        settings=Settings(repo_roots=(root,), data_dir=data),
        registry=registry,
        store=store,
        clock=lambda: NOW,
    )
    return TestClient(app, raise_server_exceptions=False)


def h(keys: dict[str, str], who: str = "alpha") -> dict[str, str]:
    return {"X-TraceMind-Key": keys[who]}


def register(
    client, keys, path: Any, name: Any = "App", who: str = "alpha", project: str | None = None
):
    project = project or ("project-demo" if who == "alpha" else "project-other")
    return client.post(
        f"/api/v1/projects/{project}/repositories",
        json={"display_name": name, "path": path},
        headers=h(keys, who),
    )


def make_repo(root: Path, name: str = "app") -> TempRepo:
    repo = TempRepo(root / name)
    repo.commit("initial", {"app/pricing.py": "x = 1\n"})
    return repo


def count_repos(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.text("SELECT count(*) FROM repositories")).scalar_one())


def test_register_and_list(client, keys, roots, engine) -> None:
    root, _ = roots
    repo = make_repo(root)
    r = register(client, keys, str(repo.path))
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["display_name"] == "App" and doc["default_branch"] == "main"
    assert doc["identity"] == "root:" + repo.git("rev-list", "--max-parents=0", "HEAD")
    assert doc["indexed_sha"] is None and doc["graph_sha256"] is None
    assert "path" not in doc and str(root) not in r.text  # server file layout is not exposed
    listing = client.get("/api/v1/projects/project-demo/repositories", headers=h(keys)).json()
    assert [i["repository_id"] for i in listing["items"]] == [doc["repository_id"]]
    assert listing["next_cursor"] is None


def test_responses_contain_no_author_information(client, keys, roots) -> None:
    root, _ = roots
    r = register(client, keys, str(make_repo(root).path))
    text = r.text + client.get("/api/v1/projects/project-demo/repositories", headers=h(keys)).text
    assert AUTHOR_NAME not in text and AUTHOR_EMAIL not in text and "hostile" not in text.lower()


def test_registration_is_read_only_for_the_repository(client, keys, roots) -> None:
    root, _ = roots
    repo = make_repo(root)
    before = tree_fingerprint(repo.path)
    assert register(client, keys, str(repo.path)).status_code == 201
    assert tree_fingerprint(repo.path) == before


def test_duplicate_registration_is_409_but_other_projects_may_register_it(
    client, keys, roots
) -> None:
    root, _ = roots
    repo = make_repo(root)
    assert register(client, keys, str(repo.path)).status_code == 201
    again = register(client, keys, str(repo.path), name="Again")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_registered"
    assert register(client, keys, str(repo.path), who="beta").status_code == 201


def hostile_cases(root: Path, tmp_path: Path) -> list[tuple[str, Any, str | None]]:
    outside = TempRepo(tmp_path / "outside")
    outside.commit("x", {"a.py": "1\n"})
    sibling = TempRepo(tmp_path / "repos-evil")
    sibling.commit("x", {"a.py": "1\n"})
    plain = root / "plain"
    plain.mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    empty = TempRepo(root / "empty")  # a repository with no commits
    assert empty.path.exists()
    return [
        ("traversal", str(root / ".." / "outside"), "outside_allowed_roots"),
        ("absolute outside", str(outside.path), "outside_allowed_roots"),
        ("name-prefix sibling", str(sibling.path), "outside_allowed_roots"),
        ("relative", "repos/app", "not_absolute"),
        ("empty", "", None),
        ("nul", str(root / "app") + "\x00", "forbidden_characters"),
        ("not found", str(root / "missing"), "not_found"),
        ("not git", str(plain), "not_a_git_repository"),
        ("a file", str(root / "file.txt"), "not_a_directory"),
        ("the root itself is no repository", str(root), "not_a_git_repository"),
        ("long", str(root / ("a" * 2000)), None),
    ]


def test_hostile_repository_paths_are_rejected_and_nothing_is_stored(
    client, keys, roots, tmp_path: Path, engine
) -> None:
    root, _ = roots
    for label, path, detail in hostile_cases(root, tmp_path):
        r = register(client, keys, path)
        assert r.status_code in (404, 422), (label, r.status_code, r.text)
        err = r.json()["error"]
        if detail:
            assert err["details"] == [{"reason": detail}], label
        assert str(root) not in r.text and "outside" not in r.text.replace("outside_allowed", ""), (
            label
        )
    assert count_repos(engine) == 0


def test_repository_without_commits_is_rejected(client, keys, roots, engine) -> None:
    root, _ = roots
    TempRepo(root / "empty")
    r = register(client, keys, str(root / "empty"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_repository"
    assert count_repos(engine) == 0


def test_symlink_or_junction_escape_is_rejected(
    client, keys, roots, tmp_path: Path, engine
) -> None:
    root, _ = roots
    secret = TempRepo(tmp_path / "secret")
    secret.commit("x", {"a.py": "1\n"})
    link = root / "innocent-looking"
    if not make_dir_link(link, secret.path):
        pytest.skip("cannot create a symlink or junction on this machine")
    r = register(client, keys, str(link))
    assert r.status_code == 422 and r.json()["error"]["details"] == [
        {"reason": "outside_allowed_roots"}
    ]
    assert count_repos(engine) == 0


def test_the_data_directory_cannot_be_registered(store, tmp_path: Path, keys, roots) -> None:
    root, data = roots
    # The allowed root deliberately contains the data directory; TraceMind's own data stays off limits.
    nested = TempRepo(data / "nested")
    nested.commit("x", {"a.py": "1" + chr(10)})
    cfg = {
        "projects": [
            {"project_id": "project-demo", "allowed_services": ["s"], "ingest_key_env": "K"}
        ]
    }
    path = tmp_path / "p3.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    registry = ConfigProjectRegistry.from_file(path, {"K": keys["alpha"]})
    app = create_app(
        settings=Settings(repo_roots=(tmp_path,), data_dir=data),
        registry=registry,
        store=store,
        clock=lambda: NOW,
    )
    r = register(TestClient(app, raise_server_exceptions=False), keys, str(nested.path))
    assert r.status_code == 422 and r.json()["error"]["details"] == [
        {"reason": "forbidden_location"}
    ]


def test_no_allowed_roots_configured_means_no_registration(
    store, tmp_path: Path, keys, roots
) -> None:
    root, data = roots
    repo = make_repo(root)
    cfg = {
        "projects": [
            {"project_id": "project-demo", "allowed_services": ["s"], "ingest_key_env": "K"}
        ]
    }
    path = tmp_path / "p2.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    registry = ConfigProjectRegistry.from_file(path, {"K": keys["alpha"]})
    app = create_app(
        settings=Settings(data_dir=data), registry=registry, store=store, clock=lambda: NOW
    )
    r = register(TestClient(app, raise_server_exceptions=False), keys, str(repo.path))
    assert r.status_code == 422 and r.json()["error"]["details"] == [
        {"reason": "no_allowed_roots_configured"}
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": "", "path": "x"},
        {"display_name": "x" * 129, "path": "x"},
        {"display_name": "ok", "path": "x", "extra": 1},
        {"display_name": 5, "path": "x"},
        {"path": "x"},
        {},
        [],
    ],
)
def test_invalid_registration_bodies_are_422(body, client, keys) -> None:
    r = client.post("/api/v1/projects/project-demo/repositories", json=body, headers=h(keys))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_control_characters_in_the_display_name_are_rejected(client, keys, roots) -> None:
    root, _ = roots
    repo = make_repo(root)
    r = register(client, keys, str(repo.path), name="bad\nname")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_display_name"


# ---- isolation and authentication --------------------------------------------------------


def test_a_projects_key_cannot_register_into_or_list_another_project(client, keys, roots) -> None:
    root, _ = roots
    repo = make_repo(root)
    assert register(client, keys, str(repo.path), who="alpha").status_code == 201
    # key beta, URL of project-demo -> 404 (existence is not revealed)
    assert (
        register(client, keys, str(repo.path), who="beta", project="project-demo").status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/projects/project-demo/repositories", headers=h(keys, "beta")
        ).status_code
        == 404
    )
    # beta's own listing never contains alpha's repository
    own = client.get("/api/v1/projects/project-other/repositories", headers=h(keys, "beta")).json()
    assert own["items"] == []
    # unknown project id
    assert client.get("/api/v1/projects/no-such/repositories", headers=h(keys)).status_code == 404


def test_repository_routes_require_a_valid_key(client, keys) -> None:
    for headers in ({}, {"X-TraceMind-Key": "wrong"}):
        assert (
            client.get("/api/v1/projects/project-demo/repositories", headers=headers).status_code
            == 401
        )
        post = client.post(
            "/api/v1/projects/project-demo/repositories",
            json={"display_name": "a", "path": "b"},
            headers=headers,
        )
        assert post.status_code == 401


def test_listing_paginates(client, keys, roots) -> None:
    root, _ = roots
    for i in range(5):
        register(client, keys, str(make_repo(root, f"app{i}").path), name=f"App {i}")
    seen: list[str] = []
    cursor = None
    for _ in range(4):
        url = "/api/v1/projects/project-demo/repositories?limit=2" + (
            f"&cursor={cursor}" if cursor else ""
        )
        page = client.get(url, headers=h(keys)).json()
        seen += [i["repository_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 5
    assert (
        client.get(
            "/api/v1/projects/project-demo/repositories?cursor=%%%", headers=h(keys)
        ).status_code
        == 400
    )


# ---- schema: append-only audit history and idempotence constraints -----------------------


def seed_scan(engine, repository_id: str, status: str = "succeeded", target: str = "a" * 40) -> str:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO repository_scans (scan_id, repository_id, project_id, kind, status, "
                "target_sha, attempt, max_attempts, available_at, queued_at) VALUES "
                "(:s, :r, 'project-demo', 'full', :st, :t, 0, 3, now(), now())"
            ),
            {"s": f"scan-{secrets.token_hex(4)}", "r": repository_id, "st": status, "t": target},
        )
        return str(
            conn.execute(
                sa.text("SELECT scan_id FROM repository_scans ORDER BY queued_at DESC LIMIT 1")
            ).scalar_one()
        )


def test_change_records_and_scan_events_are_append_only(client, keys, roots, engine) -> None:
    root, _ = roots
    repo_id = register(client, keys, str(make_repo(root).path)).json()["repository_id"]
    scan_id = seed_scan(engine, repo_id)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO change_records (record_id, repository_id, project_id, scan_id, target_sha, "
                "files, graph_delta, created_at, content_hash) VALUES "
                "('rec-1', :r, 'project-demo', :s, :t, '[]', '{}', now(), 'h')"
            ),
            {"r": repo_id, "s": scan_id, "t": "a" * 40},
        )
        conn.execute(
            sa.text(
                "INSERT INTO scan_events (scan_id, repository_id, project_id, at, event, attempt) "
                "VALUES (:s, :r, 'project-demo', now(), 'queued', 0)"
            ),
            {"r": repo_id, "s": scan_id},
        )
    for statement in (
        "UPDATE change_records SET content_hash = 'tampered'",
        "DELETE FROM change_records",
        "UPDATE scan_events SET event = 'tampered'",
        "DELETE FROM scan_events",
    ):
        with pytest.raises(DBAPIError) as info:
            with engine.begin() as conn:
                conn.execute(sa.text(statement))
        assert "append-only" in str(info.value), statement
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT content_hash FROM change_records")).scalar_one() == "h"


def test_only_one_live_and_one_successful_scan_per_target_commit(
    client, keys, roots, engine
) -> None:
    root, _ = roots
    repo_id = register(client, keys, str(make_repo(root).path)).json()["repository_id"]
    seed_scan(engine, repo_id, "queued", "b" * 40)
    with pytest.raises(DBAPIError):
        seed_scan(engine, repo_id, "running", "b" * 40)  # second live scan for the same commit
    seed_scan(engine, repo_id, "failed", "b" * 40)  # failures are history, not a conflict
    seed_scan(engine, repo_id, "succeeded", "c" * 40)
    with pytest.raises(DBAPIError):
        seed_scan(
            engine, repo_id, "succeeded", "c" * 40
        )  # a second success would duplicate the record


def test_migration_0002_creates_the_expected_tables(engine) -> None:
    names = set(sa.inspect(engine).get_table_names())
    assert {
        "repositories",
        "repository_scans",
        "change_records",
        "repository_commits",
        "scan_events",
    } <= names
