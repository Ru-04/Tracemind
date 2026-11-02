"""The Graphify runner: scrubbed environment, timeouts with process-tree kill, bounded output,
install verification, and — against the real pinned CLI — extraction, incremental update,
determinism, recovery and the read-only boundary."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from tracemind.repos.code_graph import CodeGraph
from tracemind.repos.git_adapter import GitAdapter
from tracemind.repos.graphify_runner import (
    PINNED_VERSION,
    GraphifyError,
    GraphifyRunner,
    InstallInfo,
    _scrubbed_env,
)
from tracemind.repos.snapshot import apply_changes, export_snapshot, verify_snapshot

from tests.helpers_git import TempRepo, tree_fingerprint

# ---- fake CLI (for behaviours we must provoke on demand) ---------------------------------

FAKE_SCRIPT = """
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).parent
mode = (here / "behavior.txt").read_text().strip()
(here / "env_dump.json").write_text(json.dumps({"env": dict(os.environ), "argv": sys.argv[1:], "cwd": os.getcwd()}))
if mode == "ok":
    sys.exit(0)
if mode == "fail":
    print("boom"); sys.exit(3)
if mode == "flood":
    sys.stdout.write("x" * 3_000_000); sys.exit(0)
if mode == "hang":
    time.sleep(60)
if mode == "tree":
    child = "import time, pathlib; time.sleep(3); pathlib.Path(%r).write_text('survived')" % str(here / "marker.txt")
    subprocess.Popen([sys.executable, "-c", child])
    time.sleep(60)
"""


def make_fake(tmp_path: Path, mode: str) -> tuple[GraphifyRunner, Path]:
    d = tmp_path / "fake"
    d.mkdir(exist_ok=True)
    (d / "fake.py").write_text(FAKE_SCRIPT, encoding="utf-8")
    (d / "behavior.txt").write_text(mode, encoding="utf-8")
    if os.name == "nt":
        exe = d / "graphify.cmd"
        exe.write_text(
            f'@echo off\r\n"{sys.executable}" "{d / "fake.py"}" %*\r\n', encoding="utf-8"
        )
    else:  # pragma: no cover
        exe = d / "graphify"
        exe.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{d / "fake.py"}" "$@"\n', encoding="utf-8"
        )
        exe.chmod(0o755)
    runner = GraphifyRunner(exe, home_dir=tmp_path / "home", timeout=1.5)
    runner._verified = InstallInfo(PINNED_VERSION, 1)  # the fake is not a real installation
    snap = tmp_path / "snap"
    snap.mkdir(exist_ok=True)
    return runner, snap


def dump(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "fake" / "env_dump.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_environment_is_scrubbed_of_credentials_and_proxies_point_nowhere(
    tmp_path: Path, monkeypatch
) -> None:
    for name, value in {
        "ANTHROPIC_API_KEY": "k1",
        "GROQ_API_KEY": "k2",
        "GEMINI_API_KEY": "k3",
        "OPENAI_API_KEY": "k4",
        "GITHUB_TOKEN": "k5",
        "SNOWFLAKE_PASSWORD": "k6",
        "TRACEMIND_KEY_PROJECT_DEMO": "k7",
        "TRACEMIND_DATABASE_URL": "postgresql://u:pw@h/db",
        "HTTPS_PROXY": "http://user:secret@corp-proxy:8080",
        "GRAPHIFY_FORCE": "1",
    }.items():
        monkeypatch.setenv(name, value)
    runner, snap = make_fake(tmp_path, "ok")
    runner.extract(snap)
    seen = dump(tmp_path)
    env = {k.upper(): v for k, v in seen["env"].items()}
    assert not {
        k
        for k in env
        if k.startswith(
            (
                "ANTHROPIC",
                "GROQ",
                "GEMINI",
                "OPENAI",
                "GITHUB",
                "SNOWFLAKE",
                "TRACEMIND",
                "GRAPHIFY",
            )
        )
    }
    assert "secret" not in json.dumps(seen["env"]) and "pw@" not in json.dumps(seen["env"])
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == env["ALL_PROXY"] == "http://127.0.0.1:9"
    home = str(tmp_path / "home")
    assert env["HOME"] == env["USERPROFILE"] == env["APPDATA"] == home


def test_only_the_two_documented_commands_with_the_snapshot_as_cwd(tmp_path: Path) -> None:
    runner, snap = make_fake(tmp_path, "ok")
    runner.extract(snap)
    first = dump(tmp_path)
    runner.update(snap)
    second = dump(tmp_path)
    assert first["argv"] == ["extract", ".", "--code-only", "--no-gitignore"]
    assert second["argv"] == ["update", "."]
    assert Path(first["cwd"]).resolve() == snap.resolve() == Path(second["cwd"]).resolve()


def test_nonzero_exit_is_a_failure_never_a_success(tmp_path: Path) -> None:
    runner, snap = make_fake(tmp_path, "fail")
    with pytest.raises(GraphifyError) as info:
        runner.update(snap)
    assert (info.value.code, info.value.detail) == ("graphify_failed", "exit_3")


def test_timeout_is_enforced_and_the_whole_process_tree_is_killed(tmp_path: Path) -> None:
    runner, snap = make_fake(tmp_path, "tree")
    started = time.monotonic()
    with pytest.raises(GraphifyError) as info:
        runner.extract(snap)
    assert info.value.code == "graphify_timeout"
    assert time.monotonic() - started < 20
    time.sleep(4)  # the grandchild would write its marker after 3 s if it had survived
    assert not (tmp_path / "fake" / "marker.txt").exists()


def test_a_hanging_process_times_out(tmp_path: Path) -> None:
    runner, snap = make_fake(tmp_path, "hang")
    with pytest.raises(GraphifyError) as info:
        runner.extract(snap)
    assert info.value.code == "graphify_timeout"


def test_output_capture_is_bounded(tmp_path: Path) -> None:
    runner, snap = make_fake(tmp_path, "flood")
    result = runner.extract(snap)
    assert result.exit_code == 0 and len(result.output_tail) <= 64 * 1024


def test_missing_snapshot_directory_is_a_failure(tmp_path: Path) -> None:
    runner, _ = make_fake(tmp_path, "ok")
    with pytest.raises(GraphifyError) as info:
        runner.extract(tmp_path / "does-not-exist")
    assert info.value.detail == "snapshot_missing"


# ---- installation verification (logic, with a stubbed verifier process) -------------------


def stub_verify(monkeypatch, tmp_path: Path, stdout: str) -> GraphifyRunner:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    exe = scripts / "graphify.exe"
    py = scripts / ("python.exe" if os.name == "nt" else "python")
    exe.write_text("x", encoding="utf-8")
    py.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout.encode(), stderr=b""),
    )
    return GraphifyRunner(exe, home_dir=tmp_path / "home")


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (json.dumps({"version": "0.9.97", "checked": 240, "bad": []}), None),
        (json.dumps({"version": "0.9.98", "checked": 240, "bad": []}), "graphify_version_mismatch"),
        (
            json.dumps({"version": "0.9.97", "checked": 240, "bad": ["graphify/extract.py"]}),
            "graphify_install_modified",
        ),
        (json.dumps({"version": "0.9.97", "checked": 0, "bad": []}), "graphify_install_modified"),
        ("not json at all", "graphify_unavailable"),
        ("", "graphify_unavailable"),
        (json.dumps({"version": "0.9.97"}), "graphify_unavailable"),
    ],
)
def test_installation_verification_outcomes(
    output: str, code: str | None, tmp_path: Path, monkeypatch
) -> None:
    runner = stub_verify(monkeypatch, tmp_path, output)
    if code is None:
        assert runner.verify().version == "0.9.97"
        return
    with pytest.raises(GraphifyError) as info:
        runner.verify()
    assert info.value.code == code


def test_missing_binary_is_unavailable(tmp_path: Path) -> None:
    runner = GraphifyRunner(tmp_path / "nope" / "graphify.exe", home_dir=tmp_path / "home")
    with pytest.raises(GraphifyError) as info:
        runner.verify()
    assert info.value.code == "graphify_unavailable"


def test_commands_refuse_to_run_when_verification_fails(tmp_path: Path, monkeypatch) -> None:
    runner = stub_verify(
        monkeypatch, tmp_path, json.dumps({"version": "9.9.9", "checked": 1, "bad": []})
    )
    snap = tmp_path / "snap"
    snap.mkdir()
    for call in (runner.extract, runner.update):
        with pytest.raises(GraphifyError) as info:
            call(snap)
        assert info.value.code == "graphify_version_mismatch"


def test_scrubbed_env_never_inherits_path_entries_outside_the_venv_and_system(
    tmp_path: Path,
) -> None:
    env = _scrubbed_env(tmp_path / "v" / "graphify.exe", tmp_path / "home")
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path / "v") and len(parts) <= 2


# ---- the real, pinned CLI -----------------------------------------------------------------

pytestmark_real = pytest.mark.graphify


@pytest.fixture
def real(graphify_bin: Path, tmp_path: Path) -> GraphifyRunner:
    return GraphifyRunner(graphify_bin, home_dir=tmp_path / "home", timeout=120)


@pytest.fixture
def repo(tmp_path: Path) -> TempRepo:
    r = TempRepo(tmp_path / "monitored")
    r.commit(
        "initial",
        {
            "app/pricing.py": "def average_item_price(t, q):\n    return t // q\n\n\ndef regression():\n    return average_item_price(0, 0)\n",
            "app/orders.py": "from app.pricing import average_item_price\n\n\nclass Orders:\n    def avg(self, items):\n        return average_item_price(1, len(items))\n",
            "web/util.js": "export function fmt(x) { return String(x); }\n",
            "README.md": "docs\n",
        },
    )
    return r


def snapshot_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "graphify-out" not in p.parts
    )


@pytest.mark.graphify
def test_real_install_is_the_pinned_version_with_verified_files(real: GraphifyRunner) -> None:
    info = real.verify()
    assert info.version == PINNED_VERSION == "0.9.97" and info.files_verified > 200


@pytest.mark.graphify
def test_real_extract_and_validate_a_commit_snapshot(real, repo, tmp_path: Path) -> None:
    git = GitAdapter(repo.path)
    snap = tmp_path / "snap"
    export_snapshot(git, "HEAD", snap)
    result = real.extract(snap)
    assert result.exit_code == 0
    files = snapshot_files(snap)
    graph = CodeGraph.load(snap / "graphify-out" / "graph.json", snapshot_files=files)
    assert graph.graphify_version == "0.9.97" and graph.files() == {
        "app/pricing.py",
        "app/orders.py",
        "web/util.js",
    }
    cov = graph.coverage(snap, files)
    assert cov.to_json()["complete"] and cov.not_assessed_files == 1
    (callee,) = graph.find_symbols("average_item_price")
    assert {r["file"] for r in graph.symbol_context(callee)["callers"]["items"]} == {
        "app/orders.py",
        "app/pricing.py",
    }


@pytest.mark.graphify
def test_real_builds_are_byte_identical_so_graph_hashes_prove_idempotence(
    real, repo, tmp_path: Path
) -> None:
    git = GitAdapter(repo.path)
    hashes = []
    for name in ("a", "b"):
        snap = tmp_path / name
        export_snapshot(git, "HEAD", snap)
        real.extract(snap)
        graph = CodeGraph.load(
            snap / "graphify-out" / "graph.json", snapshot_files=snapshot_files(snap)
        )
        hashes.append(graph.graph_sha256)
    assert hashes[0] == hashes[1]


@pytest.mark.graphify
def test_real_incremental_update_matches_the_commit_diff(real, repo, tmp_path: Path) -> None:
    git = GitAdapter(repo.path)
    base = repo.git("rev-parse", "HEAD")
    snap = tmp_path / "snap"
    export_snapshot(git, base, snap)
    real.extract(snap)
    old = CodeGraph.load(snap / "graphify-out" / "graph.json", snapshot_files=snapshot_files(snap))

    target = repo.commit(
        "change",
        files={
            "app/pricing.py": "def average_item_price(t, q):\n    return t // q if q else 0\n\n\ndef regression():\n    return average_item_price(0, 0)\n\n\ndef added():\n    return regression()\n",
            "app/audit.py": "from app.pricing import regression\n\n\ndef audit():\n    return regression()\n",
        },
        delete=["web/util.js"],
    )
    apply_changes(git, snap, git.diff_files(base, target))
    assert verify_snapshot(git, snap, target).ok
    real.update(snap)
    new = CodeGraph.load(snap / "graphify-out" / "graph.json", snapshot_files=snapshot_files(snap))
    delta = new.delta_from(old)
    assert set(delta.added_symbols) == {"app/pricing.py:added()", "app/audit.py:audit()"}
    assert delta.removed_symbols == ["web/util.js:fmt()"]
    assert new.files() == {"app/pricing.py", "app/orders.py", "app/audit.py"}


@pytest.mark.graphify
def test_real_update_refuses_a_corrupt_graph_and_leaves_it_untouched(
    real, repo, tmp_path: Path
) -> None:
    git = GitAdapter(repo.path)
    snap = tmp_path / "snap"
    export_snapshot(git, "HEAD", snap)
    real.extract(snap)
    graph_file = snap / "graphify-out" / "graph.json"
    graph_file.write_text('{"nodes": [', encoding="utf-8")
    with pytest.raises(GraphifyError) as info:
        real.update(snap)
    assert info.value.code == "graphify_failed"
    assert graph_file.read_text(encoding="utf-8") == '{"nodes": ['  # not silently "repaired"
    # recovery = discard the output directory and rebuild
    import shutil

    shutil.rmtree(snap / "graphify-out")
    real.extract(snap)
    CodeGraph.load(graph_file, snapshot_files=snapshot_files(snap))


@pytest.mark.graphify
def test_real_syntax_error_is_silent_in_graphify_but_visible_in_our_coverage(
    real, repo, tmp_path: Path
) -> None:
    repo.commit("break", {"app/broken.py": "def broken(:\n    pass\n"})
    git = GitAdapter(repo.path)
    snap = tmp_path / "snap"
    export_snapshot(git, "HEAD", snap)
    assert real.extract(snap).exit_code == 0  # Graphify does not complain
    files = snapshot_files(snap)
    graph = CodeGraph.load(snap / "graphify-out" / "graph.json", snapshot_files=files)
    cov = graph.coverage(snap, files)
    assert cov.python_syntax_errors == ["app/broken.py"] and not cov.to_json()["complete"]


@pytest.mark.graphify
def test_real_gitignore_in_the_repository_does_not_hide_tracked_files(
    real, repo, tmp_path: Path
) -> None:
    repo.commit("ignore rule", {".gitignore": "app/orders.py\n"})
    git = GitAdapter(repo.path)
    snap = tmp_path / "snap"
    export_snapshot(git, "HEAD", snap)
    real.extract(snap)
    files = snapshot_files(snap)
    graph = CodeGraph.load(snap / "graphify-out" / "graph.json", snapshot_files=files)
    assert "app/orders.py" in graph.files()


@pytest.mark.graphify
def test_real_run_writes_only_inside_the_snapshot_and_home_and_never_touches_the_repository(
    real, repo, tmp_path: Path
) -> None:
    git = GitAdapter(repo.path)
    snap = tmp_path / "snap"
    export_snapshot(git, "HEAD", snap)
    repo_before = tree_fingerprint(repo.path)
    others = {"monitored", "snap", "home"}
    before = {
        k: v
        for k, v in tree_fingerprint(tmp_path).items()
        if k.split("/")[0] not in {"snap", "home"}
    }
    real.extract(snap)
    real.update(snap)
    after = {
        k: v
        for k, v in tree_fingerprint(tmp_path).items()
        if k.split("/")[0] not in {"snap", "home"}
    }
    assert after == before and others
    assert tree_fingerprint(repo.path) == repo_before
    assert (snap / "graphify-out" / "graph.json").exists()
