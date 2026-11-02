"""S5: the repository API end to end — scans, status, changes, code — with project isolation,
hostile inputs, honest labelling of derived data, and the M2 demo script."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from tracemind.api.app import create_app
from tracemind.projects import ConfigProjectRegistry
from tracemind.repos.errors import RepoError
from tracemind.storage.postgres import PostgresStore

from tests.helpers_git import AUTHOR_EMAIL, AUTHOR_NAME, TempRepo
from tests.integration.scan_helpers import FaultyRunner, make_repo

pytestmark = [pytest.mark.integration, pytest.mark.graphify]

P = "project-demo"


@pytest.fixture
def keys() -> dict[str, str]:
    return {"alpha": secrets.token_urlsafe(32), "beta": secrets.token_urlsafe(32)}


@pytest.fixture
def api(env, keys, tmp_path: Path) -> TestClient:
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
        settings=env.settings,
        registry=registry,
        store=PostgresStore(env.engine),
        repositories=env.repos,
        scan_service=env.service,
        clock=env.clock,
    )
    return TestClient(app, raise_server_exceptions=False)


def h(keys, who: str = "alpha") -> dict[str, str]:
    return {"X-TraceMind-Key": keys[who]}


def url(repo_id: str, tail: str = "", project: str = P) -> str:
    return f"/api/v1/projects/{project}/repositories/{repo_id}{tail}"


def register(api, keys, repo: TempRepo, who: str = "alpha", project: str = P) -> str:
    r = api.post(
        f"/api/v1/projects/{project}/repositories",
        json={"display_name": "App", "path": str(repo.path)},
        headers=h(keys, who),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["repository_id"])


def scan_and_work(api, keys, env, repo_id: str, revision: str | None = None):
    body = {"revision": revision} if revision else None
    r = api.post(url(repo_id, "/scans"), json=body, headers=h(keys))
    env.run_all()
    return r


# ---- the M2 deliverable: a code change updates the index and produces an auditable record --


def test_demo_script_code_change_updates_index_and_produces_a_change_record(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)

    # 1. before any scan
    st = api.get(url(repo_id, "/status"), headers=h(keys)).json()
    assert (
        st["index_state"] == "never_indexed" and st["indexed_sha"] is None and st["graph"] is None
    )
    assert api.get(url(repo_id, "/code?symbol=regression"), headers=h(keys)).status_code == 409

    # 2. initial scan
    r = scan_and_work(api, keys, env, repo_id)
    assert (
        r.status_code == 202
        and r.json()["created"] is True
        and r.json()["scan"]["status"] == "queued"
    )
    first_sha = repo.git("rev-parse", "HEAD")
    st = api.get(url(repo_id, "/status"), headers=h(keys)).json()
    assert st["index_state"] == "indexed" and st["indexed_sha"] == first_sha
    assert st["graph"]["derived_data"] is True and st["graph"]["tool"] == "graphify"
    assert st["graph"]["tool_version"] == "0.9.97" and len(st["graph"]["sha256"]) == 64
    assert st["last_successful_scan"]["kind"] == "full" and st["last_scan"]["status"] == "succeeded"
    first_graph = st["graph"]["sha256"]

    # 3. the real code change: add a function, edit another, delete a file
    new_sha = repo.commit(
        "add audit, guard pricing, drop js",
        files={
            "app/audit.py": "from app.pricing import regression\n\n\ndef audit():\n    return regression()\n",
            "app/pricing.py": "def average_item_price(total, qty):\n    return total // qty if qty else 0\n\n\ndef regression():\n    return average_item_price(0, 0)\n",
        },
        delete=["web/util.js"],
    )
    r = scan_and_work(api, keys, env, repo_id)
    assert r.status_code == 202 and r.json()["scan"]["target_sha"] == new_sha

    # 4. the auditable change record
    changes = api.get(url(repo_id, "/changes"), headers=h(keys)).json()
    assert len(changes["items"]) == 2
    rec = changes["items"][0]  # newest first
    assert (rec["base_sha"], rec["target_sha"]) == (first_sha, new_sha)
    assert {(f["path"], f["status"]) for f in rec["files"]["items"]} == {
        ("app/audit.py", "A"),
        ("app/pricing.py", "M"),
        ("web/util.js", "D"),
    }
    pricing = next(f for f in rec["files"]["items"] if f["path"] == "app/pricing.py")
    assert pricing["additions"] is not None and pricing["deletions"] is not None
    assert [c["sha"] for c in rec["commits"]] == [new_sha]
    assert rec["commits"][0]["subject"] == "add audit, guard pricing, drop js"
    assert "app/audit.py:audit()" in rec["graph_delta"]["added_symbols"]
    assert "web/util.js:fmt()" in rec["graph_delta"]["removed_symbols"]
    assert rec["graph_sha256"] != first_graph and len(rec["content_hash"]) == 64
    assert (
        rec["scan_id"]
        == api.get(url(repo_id, "/status"), headers=h(keys)).json()["last_successful_scan"][
            "scan_id"
        ]
    )

    # 5. the index reflects the change
    code = api.get(url(repo_id, "/code?symbol=regression"), headers=h(keys)).json()
    (ctx,) = code["symbols"]
    callers = {(c["file"], c["label"]) for c in ctx["callers"]["items"]}
    assert ("app/audit.py", "audit()") in callers
    assert all(
        c["confidence"] == "EXTRACTED" and c["evidence_line"] for c in ctx["callers"]["items"]
    )
    assert (
        code["provenance"]["indexed_sha"] == new_sha and code["provenance"]["derived_data"] is True
    )
    st = api.get(url(repo_id, "/status"), headers=h(keys)).json()
    assert st["indexed_sha"] == new_sha and st["graph"]["sha256"] == rec["graph_sha256"]
    gone = api.get(url(repo_id, "/code?file=web/util.js"), headers=h(keys))
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_in_graph"


# ---- POST /scans -------------------------------------------------------------------------


def test_queueing_is_idempotent_and_validates_input(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    first = api.post(url(repo_id, "/scans"), headers=h(keys))
    again = api.post(url(repo_id, "/scans"), headers=h(keys))
    assert (first.status_code, again.status_code) == (202, 200)
    assert (
        first.json()["scan"]["scan_id"] == again.json()["scan"]["scan_id"]
        and again.json()["created"] is False
    )
    for revision in ("--upload-pack=x", "HEAD~1", "main..HEAD", "a b", "$(id)", "x" * 300):
        r = api.post(url(repo_id, "/scans"), json={"revision": revision}, headers=h(keys))
        assert r.status_code in (422,), (revision, r.status_code)
    r = api.post(url(repo_id, "/scans"), json={"revision": "no-such-branch"}, headers=h(keys))
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    for body in ({"revision": 5}, {"revision": ""}, {"extra": 1}):
        assert api.post(url(repo_id, "/scans"), json=body, headers=h(keys)).status_code == 422
    r = api.post(url("no-such-repo", "/scans"), headers=h(keys))
    assert r.status_code == 404
    env.run_all()
    done = api.post(url(repo_id, "/scans"), headers=h(keys)).json()
    assert done["created"] is False and done["scan"]["status"] == "succeeded"


def test_status_states_follow_the_scan_lifecycle_including_failure(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    api.post(url(repo_id, "/scans"), headers=h(keys))
    assert (
        api.get(url(repo_id, "/status"), headers=h(keys)).json()["index_state"]
        == "never_indexed_scan_pending"
    )
    env.run_all()
    assert api.get(url(repo_id, "/status"), headers=h(keys)).json()["index_state"] == "indexed"
    good = api.get(url(repo_id, "/status"), headers=h(keys)).json()

    repo.commit("edit", {"app/pricing.py": "def average_item_price(t, q):\n    return 1\n"})
    api.post(url(repo_id, "/scans"), headers=h(keys))
    assert (
        api.get(url(repo_id, "/status"), headers=h(keys)).json()["index_state"]
        == "indexed_scan_pending"
    )

    from tracemind.repos.graphify_runner import GraphifyError

    from tests.integration.scan_helpers import FaultyRunner

    def fail(kind, d, n):
        raise GraphifyError("graphify_failed", "exit_1")

    failing = env.with_runner(FaultyRunner(env.real_runner, before=fail))
    while failing.process_next("w") is not None:
        pass
    st = api.get(url(repo_id, "/status"), headers=h(keys)).json()
    assert st["index_state"] == "indexed_last_scan_failed"
    assert (
        st["last_scan"]["status"] == "failed" and st["last_scan"]["error_code"] == "graphify_failed"
    )
    assert (
        st["indexed_sha"] == good["indexed_sha"]
        and st["graph"]["sha256"] == good["graph"]["sha256"]
    )
    # and the previous graph still answers queries
    assert api.get(url(repo_id, "/code?symbol=regression"), headers=h(keys)).status_code == 200


# ---- changes -----------------------------------------------------------------------------


def test_changes_paginate_and_filter_by_a_verified_commit(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    shas = [repo.git("rev-parse", "HEAD")]
    for i in range(3):
        shas.append(repo.commit(f"c{i}", {f"f{i}.py": f"x = {i}\n"}))
        scan_and_work(api, keys, env, repo_id)
    page1 = api.get(url(repo_id, "/changes?limit=2"), headers=h(keys)).json()
    page2 = api.get(
        url(repo_id, f"/changes?limit=2&cursor={page1['next_cursor']}"), headers=h(keys)
    ).json()
    seen = [r["target_sha"] for r in page1["items"] + page2["items"]]
    assert seen == list(reversed(shas)) and page2["next_cursor"] is None
    assert "release_mapping" not in page1  # only present when asked for
    only = api.get(url(repo_id, f"/changes?commit_sha={shas[2]}"), headers=h(keys)).json()
    assert [r["target_sha"] for r in only["items"]] == [shas[2]]
    assert only["release_mapping"] == {
        "commit_sha": shas[2],
        "confidence": "verified",
        "source": "telemetry.commit_sha",
        "reason": None,
    }
    assert api.get(url(repo_id, "/changes?cursor=%%%"), headers=h(keys)).status_code == 400


def test_unknown_release_mappings_stay_unknown_and_return_no_changes(api, env, keys) -> None:
    repo = make_repo(env)
    repo.git("tag", "v1.2.0")
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    head = repo.git("rev-parse", "HEAD")
    cases = {
        "release=v1.2.0": "no_commit_in_telemetry",  # a tag with the same name is NOT used
        "release=main": "no_commit_in_telemetry",
        "release=1.2.0&commit_sha=": "no_commit_in_telemetry",
        f"commit_sha={head[:8]}": "abbreviated_sha",
        f"commit_sha={'0' * 40}": "commit_not_in_repository",
        "commit_sha=HEAD": "invalid_commit_sha",
        "commit_sha=main": "invalid_commit_sha",
        "commit_sha=--upload-pack%3Dx": "invalid_commit_sha",
        "commit_sha=v1.2.0": "invalid_commit_sha",
    }
    for query, reason in cases.items():
        r = api.get(url(repo_id, f"/changes?{query}"), headers=h(keys))
        assert r.status_code == 200, (query, r.text)
        doc = r.json()
        assert doc["items"] == [] and doc["next_cursor"] is None, query
        assert doc["release_mapping"] == {
            "commit_sha": None,
            "confidence": "unknown",
            "source": None,
            "reason": reason,
        }, query
    # a repository that is registered but whose release metadata is absent never gains a mapping
    r = api.get(url(repo_id, f"/changes?release=1.2.0&commit_sha={head}"), headers=h(keys)).json()
    assert r["release_mapping"]["confidence"] == "verified" and len(r["items"]) == 1


# ---- code --------------------------------------------------------------------------------


def test_code_file_and_symbol_context_with_labels(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    f = api.get(url(repo_id, "/code?file=app/orders.py"), headers=h(keys)).json()
    assert (
        f["provenance"]["tool_version"] == "0.9.97" and f["provenance"]["coverage_complete"] is True
    )
    assert {r["file"] for r in f["file_context"]["imports"]["items"]} == {"app/pricing.py"}
    assert all(
        r["confidence"] in ("EXTRACTED", "INFERRED", "UNKNOWN")
        for k in ("imports", "calls_out")
        for r in f["file_context"][k]["items"]
    )
    s = api.get(url(repo_id, "/code?symbol=average_item_price"), headers=h(keys)).json()
    assert [m["symbol"]["file"] for m in s["symbols"]] == ["app/pricing.py"]
    assert "confidence_legend" in s["provenance"]
    missing = api.get(url(repo_id, "/code?file=README.md"), headers=h(keys))
    assert (
        missing.status_code == 404
        and "does not mean it is irrelevant" in missing.json()["error"]["message"]
    )


def test_inferred_edges_are_returned_labelled_never_as_facts(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    graphs = env.data_dir / "repos" / repo_id / "graphs"
    sha = env.repo(repo_id)["graph_sha256"]
    doc = json.loads((graphs / f"{sha}.json").read_text(encoding="utf-8"))
    for edge in doc["links"]:
        if edge["relation"] == "calls":
            edge["confidence"], edge["confidence_score"] = "INFERRED", 0.4
    raw = json.dumps(doc).encode()
    new_sha = hashlib.sha256(raw).hexdigest()
    (graphs / f"{new_sha}.json").write_bytes(raw)
    (graphs / f"{new_sha}.files.json").write_bytes((graphs / f"{sha}.files.json").read_bytes())
    with env.engine.begin() as conn:
        conn.execute(sa.text("UPDATE repositories SET graph_sha256 = :s"), {"s": new_sha})
    s = api.get(url(repo_id, "/code?symbol=average_item_price"), headers=h(keys)).json()
    rows = s["symbols"][0]["callers"]["items"]
    assert rows and all(
        r["confidence"] == "INFERRED" and r["confidence_score"] == 0.4 for r in rows
    )


def test_a_tampered_published_graph_is_refused_not_served(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    graphs = env.data_dir / "repos" / repo_id / "graphs"
    sha = env.repo(repo_id)["graph_sha256"]
    (graphs / f"{sha}.json").write_text(
        '{"nodes": []}', encoding="utf-8"
    )  # content no longer matches its hash
    env.service._graph_cache.clear()  # noqa: SLF001
    r = api.get(url(repo_id, "/code?symbol=regression"), headers=h(keys))
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_indexed"


def test_incomplete_indexing_is_flagged(api, env, keys) -> None:
    repo = make_repo(env)
    repo.commit("break", {"app/broken.py": "def broken(:\n    pass\n"})
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    st = api.get(url(repo_id, "/status"), headers=h(keys)).json()
    assert st["graph"]["coverage"]["complete"] is False
    assert st["graph"]["coverage"]["python_syntax_errors"] == ["app/broken.py"]
    code = api.get(url(repo_id, "/code?file=app/broken.py"), headers=h(keys)).json()
    assert code["provenance"]["coverage_complete"] is False
    assert any("incomplete" in w for w in code["warnings"])


@pytest.mark.parametrize(
    "file",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:%5CWindows",
        ".git/config",
        "a/../../b",
        "-rf",
        "a%00b",
        "file.py:stream",
        "x" * 1025,
        "a%5Cb",
    ],
)
def test_hostile_file_parameters_are_rejected_without_touching_the_filesystem(
    file, api, env, keys, monkeypatch
) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    opened: list[object] = []
    real_open = Path.open

    def spy_open(self: Path, *a: Any, **k: Any) -> Any:
        opened.append(self)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy_open)
    r = api.get(url(repo_id, f"/code?file={file}"), headers=h(keys))
    assert r.status_code in (404, 422), (file, r.status_code)
    assert r.status_code == 422 or r.json()["error"]["code"] == "not_in_graph"
    assert "passwd" not in r.text and "win.ini" not in r.text
    graphs_root = (env.data_dir / "repos").resolve()
    # Only TraceMind's own content-addressed graph files may be opened, never a caller-named path;
    # and input rejected by validation (422) opens nothing at all.
    assert all(
        graphs_root in Path(str(p)).resolve().parents and "graphs" in Path(str(p)).parts
        for p in opened
    )
    if r.status_code == 422:
        assert opened == []


@pytest.mark.parametrize(
    "symbol", ["*", ".*", "a|b", "'; DROP TABLE x;--", "x" * 200, "../../x", "{{7*7}}"]
)
def test_symbol_lookup_is_an_exact_name_match_never_a_pattern_or_query(
    symbol, api, env, keys
) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    r = api.get(
        url(
            repo_id,
            "/code",
        ).replace("/code", "/code")
        + "?symbol="
        + symbol,
        headers=h(keys),
    )
    assert r.status_code in (404, 422), (symbol, r.text)
    assert r.json()["error"]["code"] in ("not_in_graph", "invalid_query", "validation_error")


def test_code_requires_exactly_one_of_file_or_symbol(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    for q in ("", "?file=app/orders.py&symbol=regression"):
        r = api.get(url(repo_id, "/code" + q), headers=h(keys))
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_query"
    assert api.get(url(repo_id, "/code?symbol=" + "x" * 201), headers=h(keys)).status_code == 422


# ---- isolation, authentication, leakage --------------------------------------------------

ENDPOINTS = [
    ("get", "/status"),
    ("get", "/changes"),
    ("get", "/code?symbol=regression"),
    ("post", "/scans"),
]


def test_another_projects_key_sees_nothing(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    scan_and_work(api, keys, env, repo_id)
    for method, tail in ENDPOINTS:
        # right URL project, wrong (other project's) repository id
        r = getattr(api, method)(url(repo_id, tail, "project-other"), headers=h(keys, "beta"))
        assert r.status_code == 404, (method, tail)
        # beta's key against alpha's URL
        r = getattr(api, method)(url(repo_id, tail), headers=h(keys, "beta"))
        assert r.status_code == 404, (method, tail)
    own = api.get("/api/v1/projects/project-other/repositories", headers=h(keys, "beta")).json()
    assert own["items"] == []


def test_all_repository_routes_require_a_key(api, env, keys) -> None:
    repo = make_repo(env)
    repo_id = register(api, keys, repo)
    for method, tail in ENDPOINTS:
        for headers in ({}, {"X-TraceMind-Key": "wrong"}):
            r = getattr(api, method)(url(repo_id, tail), headers=headers)
            assert r.status_code == 401, (method, tail)


def test_responses_never_leak_paths_authors_or_secrets(api, env, keys) -> None:
    repo = make_repo(env)
    repo.commit("fix login password=hunter2-correct-horse", {"app/x.py": "a = 1\n"})
    repo_id = register(api, keys, repo)
    texts = [
        scan_and_work(api, keys, env, repo_id).text,
        api.get(url(repo_id, "/status"), headers=h(keys)).text,
        api.get(url(repo_id, "/changes"), headers=h(keys)).text,
        api.get(url(repo_id, "/code?file=app/x.py"), headers=h(keys)).text,
        api.get(url(repo_id, "/code?symbol=regression"), headers=h(keys)).text,
        api.get("/api/v1/projects/project-demo/repositories", headers=h(keys)).text,
    ]
    blob = " ".join(texts)
    for needle in (
        str(env.root),
        str(env.data_dir),
        str(repo.path),
        "graphify-out",
        AUTHOR_NAME,
        AUTHOR_EMAIL,
        "hostile",
        "hunter2",
        "Users\\\\",
        ".graphify-venv",
    ):
        assert needle not in blob, needle
    assert "[REDACTED:" in blob  # the commit subject was redacted before storage
    for key in keys.values():
        assert key not in blob


def test_the_api_process_can_enqueue_without_a_graphify_installation(
    env, keys, tmp_path: Path
) -> None:
    """The API never runs Graphify; only the worker does."""
    from tracemind.config import Settings

    cfg = {
        "projects": [
            {"project_id": "project-demo", "allowed_services": ["s"], "ingest_key_env": "K"}
        ]
    }
    path = tmp_path / "p.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    registry = ConfigProjectRegistry.from_file(path, {"K": keys["alpha"]})
    app = create_app(
        settings=Settings(repo_roots=(env.root,), data_dir=env.data_dir),  # no graphify_bin
        registry=registry,
        store=PostgresStore(env.engine),
        clock=env.clock,
    )
    client = TestClient(app, raise_server_exceptions=False)
    repo = make_repo(env)
    repo_id = register(client, keys, repo)
    assert client.post(url(repo_id, "/scans"), headers=h(keys)).status_code == 202
    # a process without Graphify refuses to run the scan, with a stable non-retryable code
    from tracemind.repos.scan_service import ScanService
    from tracemind.repos.scan_store import ScanStore

    blind = ScanService(
        repositories=env.repos,
        scans=ScanStore(env.engine),
        runner=None,
        settings=env.settings,
        clock=env.clock,
    )
    res = blind.process_next("w")
    assert res is not None and res.status == "failed" and res.error_code == "graphify_unavailable"


def test_repo_error_codes_map_to_stable_http_statuses() -> None:
    from tracemind.api.repos import repo_error_to_api

    assert repo_error_to_api(RepoError("not_found")).status == 404
    assert repo_error_to_api(RepoError("not_indexed")).status == 409
    assert repo_error_to_api(RepoError("invalid_revision", "forbidden_characters")).status == 422
    assert repo_error_to_api(RepoError("snapshot_inconsistent")).status == 500
    assert FaultyRunner is not None
