"""M3-11 and M3-10 (deployments half): validation, project scoping, idempotence, immutability and
the commit link, which is `verified` only for a full SHA that exists in a registered repository
of the same project and is never inferred from time."""

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
from tests.helpers_git import TempRepo

pytestmark = pytest.mark.integration

A = "project-demo"
B = "project-other"
WHEN = "2026-10-28T09:00:00Z"


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
            {"project_id": A, "allowed_services": ["payments-api"], "ingest_key_env": "KA"},
            {"project_id": B, "allowed_services": ["payments-api"], "ingest_key_env": "KB"},
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


def pid(who: str) -> str:
    return A if who == "alpha" else B


def deploy(client, keys, who: str = "alpha", project: str | None = None, **body: Any):
    payload = {"environment": "production", "release": "2026.10.28", "deployed_at": WHEN, **body}
    return client.post(
        f"/api/v1/projects/{project or pid(who)}/deployments", json=payload, headers=h(keys, who)
    )


def listing(client, keys, who: str = "alpha", **params: Any):
    return client.get(
        f"/api/v1/projects/{pid(who)}/deployments", params=params, headers=h(keys, who)
    )


def rows(engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.text("SELECT * FROM deployments")).mappings())


def register_repo(client, keys, root: Path, who: str = "alpha", name: str = "app"):
    repo = TempRepo(root / f"{who}-{name}")
    sha = repo.commit("initial", {"app/pricing.py": "x = 1\n"})
    r = client.post(
        f"/api/v1/projects/{pid(who)}/repositories",
        json={"display_name": name, "path": str(repo.path)},
        headers=h(keys, who),
    )
    assert r.status_code == 201, r.text
    return repo, sha, r.json()["repository_id"]


# ---- recording and reading ------------------------------------------------------------------


def test_record_and_list(client, keys, engine) -> None:
    r = deploy(client, keys, service="Payments-API", environment="Production")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["project_id"] == A and doc["environment"] == "production"
    assert doc["service"] == "payments-api" and doc["release"] == "2026.10.28"
    assert doc["deployed_at"] == WHEN and doc["source"] == "api"
    assert doc["deployment_id"].startswith("dep-")
    assert doc["commit_association"] == {
        "commit_sha": None,
        "confidence": "unknown",
        "source": None,
        "reason": "no_commit_in_telemetry",
        "reported": None,
    }
    assert listing(client, keys).json() == {"items": [doc], "next_cursor": None}


def test_listing_is_newest_first_filterable_and_paginated(client, keys) -> None:
    for i in range(5):
        deploy(client, keys, release=f"r{i}", deployed_at=f"2026-10-28T0{i}:00:00Z")
    deploy(
        client, keys, release="staging-1", environment="staging", deployed_at="2026-10-27T00:00:00Z"
    )
    first = listing(client, keys, limit=3).json()
    assert [i["release"] for i in first["items"]] == ["r4", "r3", "r2"] and first["next_cursor"]
    rest = listing(client, keys, limit=3, cursor=first["next_cursor"]).json()
    assert [i["release"] for i in rest["items"]] == ["r1", "r0", "staging-1"]
    assert rest["next_cursor"] is None
    assert [i["release"] for i in listing(client, keys, environment="STAGING").json()["items"]] == [
        "staging-1"
    ]
    assert [i["release"] for i in listing(client, keys, release="r2").json()["items"]] == ["r2"]
    assert listing(client, keys, cursor="@@@").status_code == 400


def test_same_record_twice_is_idempotent(client, keys, engine) -> None:
    first = deploy(client, keys, service="payments-api")
    again = deploy(client, keys, service="payments-api")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert len(rows(engine)) == 1


def test_explicit_id_with_same_content_is_idempotent_with_different_content_is_a_conflict(
    client, keys, engine
) -> None:
    ok = deploy(client, keys, deployment_id="deploy-42")
    assert ok.status_code == 201 and ok.json()["deployment_id"] == "deploy-42"
    assert deploy(client, keys, deployment_id="deploy-42").status_code == 200
    clash = deploy(client, keys, deployment_id="deploy-42", release="something-else")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "deployment_conflict"
    assert "something-else" not in clash.text
    stored = rows(engine)
    assert len(stored) == 1 and stored[0]["release"] == "2026.10.28"  # the original wins


def test_the_same_id_in_two_projects_is_two_records(client, keys, engine) -> None:
    assert deploy(client, keys, "alpha", deployment_id="same").status_code == 201
    assert deploy(client, keys, "beta", deployment_id="same").status_code == 201
    assert len(rows(engine)) == 2
    assert len(listing(client, keys, "alpha").json()["items"]) == 1


# ---- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code", "field"),
    [
        ({"environment": "Prod uction"}, "invalid_environment", "environment"),
        ({"environment": "../etc"}, "invalid_environment", "environment"),
        ({"service": "unknown-svc"}, "unknown_service", "service"),
        ({"service": "Bad Service!"}, "invalid_service", "service"),
        ({"release": "   "}, "missing_field", "release"),
        ({"release": "bad\nrelease"}, "invalid_release", "release"),
        ({"deployed_at": "yesterday"}, "invalid_deployed_at", "deployed_at"),
        ({"deployed_at": "2026-10-28 09:00:00"}, "invalid_deployed_at", "deployed_at"),
        ({"deployed_at": "2030-01-01T00:00:00Z"}, "deployed_at_in_future", "deployed_at"),
        ({"deployment_id": "has space"}, "invalid_deployment_id", "deployment_id"),
        ({"commit_sha": "bad\x00sha"}, "invalid_commit_sha", "commit_sha"),
    ],
)
def test_invalid_requests_are_rejected_without_echo_and_store_nothing(
    client, keys, engine, body: dict[str, Any], code: str, field: str
) -> None:
    r = deploy(client, keys, **body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == code and err["details"] == [{"field": field}]
    for value in body.values():
        assert str(value) not in r.text
    assert rows(engine) == []


def test_request_shape_is_strict(client, keys, engine) -> None:
    url = f"/api/v1/projects/{A}/deployments"
    base = {"environment": "production", "release": "r1", "deployed_at": WHEN}
    for bad in (
        {**base, "extra": 1},
        {k: v for k, v in base.items() if k != "release"},
        {**base, "release": 5},
        {**base, "commit_sha": 5},
        {**base, "release": "x" * 129},
    ):
        assert client.post(url, json=bad, headers=h(keys)).status_code == 422, bad
    assert client.post(url, content=b"[]", headers=h(keys)).status_code == 422
    assert rows(engine) == []


def test_deployment_in_the_near_future_within_clock_skew_is_accepted(client, keys) -> None:
    assert deploy(client, keys, deployed_at="2026-10-28T12:05:00Z").status_code == 201


def test_offsets_are_normalized_to_utc(client, keys) -> None:
    r = deploy(client, keys, deployed_at="2026-10-28T11:00:00+02:00")
    assert r.json()["deployed_at"] == "2026-10-28T09:00:00Z"


# ---- project scoping and authentication ---------------------------------------------------------


def test_another_project_cannot_read_or_write_through_a_foreign_path(client, keys, engine) -> None:
    deploy(client, keys, "alpha")
    # beta's key against alpha's path, for reading and writing, is a 404 (existence not revealed)
    assert (
        client.get(f"/api/v1/projects/{A}/deployments", headers=h(keys, "beta")).status_code == 404
    )
    assert deploy(client, keys, "beta", project=A).status_code == 404
    assert len(rows(engine)) == 1
    assert listing(client, keys, "beta").json() == {"items": [], "next_cursor": None}


def test_requests_without_a_valid_key_are_rejected(client, keys) -> None:
    url = f"/api/v1/projects/{A}/deployments"
    assert client.get(url).status_code == 401
    assert client.post(url, json={}).status_code == 401
    assert client.get(url, headers={"X-TraceMind-Key": "nope"}).status_code == 401


def test_records_cannot_be_updated_or_deleted(client, keys, engine) -> None:
    deploy(client, keys)
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE deployments SET release = 'edited'"))
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM deployments"))
    assert rows(engine)[0]["release"] == "2026.10.28"


def test_database_refuses_a_verified_link_without_a_commit(engine) -> None:
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO deployments (project_id, deployment_id, environment, release, "
                "deployed_at, commit_confidence, source, created_at, content_hash) VALUES "
                "('p', 'd', 'prod', 'r', now(), 'verified', 'api', now(), 'h')"
            )
        )


# ---- the commit link (M3-11) ---------------------------------------------------------------


def test_full_sha_in_the_named_repository_is_verified(client, keys, roots) -> None:
    repo, sha, repo_id = register_repo(client, keys, roots[0])
    r = deploy(client, keys, commit_sha=sha, repository_id=repo_id)
    assert r.status_code == 201, r.text
    assert r.json()["commit_association"] == {
        "commit_sha": sha,
        "confidence": "verified",
        "source": "telemetry.commit_sha",
        "reason": None,
        "reported": sha,
    }
    assert r.json()["repository_id"] == repo_id


def test_uppercase_full_sha_is_verified_in_lowercase(client, keys, roots) -> None:
    _, sha, repo_id = register_repo(client, keys, roots[0])
    r = deploy(client, keys, commit_sha=sha.upper(), repository_id=repo_id)
    assoc = r.json()["commit_association"]
    assert assoc["confidence"] == "verified" and assoc["commit_sha"] == sha
    assert assoc["reported"] == sha.upper()  # what was reported is kept as reported


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda sha: sha[:12], "abbreviated_sha"),
        (lambda sha: "a" * 40, "commit_not_in_repository"),
        (lambda sha: "main", "invalid_commit_sha"),
        (lambda sha: "HEAD", "invalid_commit_sha"),
        (lambda sha: f"{sha}~1", "invalid_commit_sha"),
        (lambda sha: "z" * 40, "invalid_commit_sha"),
    ],
)
def test_anything_but_an_existing_full_sha_is_unknown(client, keys, roots, make, reason) -> None:
    _, sha, repo_id = register_repo(client, keys, roots[0])
    r = deploy(client, keys, commit_sha=make(sha), repository_id=repo_id)
    assert r.status_code == 201
    assoc = r.json()["commit_association"]
    assert (assoc["confidence"], assoc["commit_sha"], assoc["reason"]) == ("unknown", None, reason)


def test_a_commit_without_a_repository_is_unknown(client, keys, roots) -> None:
    _, sha, _ = register_repo(client, keys, roots[0])
    assoc = deploy(client, keys, commit_sha=sha).json()["commit_association"]
    assert (assoc["confidence"], assoc["commit_sha"], assoc["reason"]) == (
        "unknown",
        None,
        "no_repository",
    )


def test_release_and_time_never_produce_a_link(client, keys, roots) -> None:
    repo, sha, repo_id = register_repo(client, keys, roots[0])
    sha2 = repo.commit("second", {"app/pricing.py": "x = 2\n"})
    # a release named after the commit, deployed right after it, with a repository named
    r = deploy(
        client, keys, release=sha2, repository_id=repo_id, deployed_at="2026-10-28T10:02:00Z"
    )
    assoc = r.json()["commit_association"]
    assert (assoc["confidence"], assoc["commit_sha"]) == ("unknown", None)
    assert assoc["reason"] == "no_commit_in_telemetry"
    assert sha != sha2


def test_another_projects_repository_is_not_found_and_stores_nothing(
    client, keys, roots, engine
) -> None:
    _, sha, beta_repo = register_repo(client, keys, roots[0], who="beta")
    r = deploy(client, keys, "alpha", commit_sha=sha, repository_id=beta_repo)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    missing = deploy(client, keys, "alpha", commit_sha=sha, repository_id="no-such-repo")
    assert (
        missing.status_code == 404
        and missing.json()["error"]["message"] == r.json()["error"]["message"]
    )
    assert rows(engine) == []


def test_an_unavailable_repository_yields_unknown_not_an_error(client, keys, roots) -> None:
    repo, sha, repo_id = register_repo(client, keys, roots[0])
    repo.path.rename(repo.path.with_name("moved-away"))
    assoc = deploy(client, keys, commit_sha=sha, repository_id=repo_id).json()["commit_association"]
    assert (assoc["confidence"], assoc["reason"]) == ("unknown", "repository_unavailable")


def test_the_link_is_fixed_when_the_record_is_written(client, keys, roots, engine) -> None:
    """A commit that did not exist yet stays unknown on a retry: records are immutable."""
    repo, _, repo_id = register_repo(client, keys, roots[0])
    future = "b" * 40
    first = deploy(client, keys, commit_sha=future, repository_id=repo_id)
    assert first.json()["commit_association"]["reason"] == "commit_not_in_repository"
    again = deploy(client, keys, commit_sha=future, repository_id=repo_id)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(rows(engine)) == 1


def test_the_repository_is_not_modified_and_no_author_data_is_returned(client, keys, roots) -> None:
    from tests.helpers_git import AUTHOR_EMAIL, AUTHOR_NAME, tree_fingerprint

    repo, sha, repo_id = register_repo(client, keys, roots[0])
    before = tree_fingerprint(repo.path)
    r = deploy(client, keys, commit_sha=sha, repository_id=repo_id)
    assert tree_fingerprint(repo.path) == before
    assert AUTHOR_NAME not in r.text and AUTHOR_EMAIL not in r.text
    assert str(roots[0]) not in r.text
