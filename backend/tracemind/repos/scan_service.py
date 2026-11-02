"""Scan orchestration: snapshot → Graphify → validate → publish, with recovery.

Guarantees (each covered by tests):
- the monitored repository is only ever read; Graphify runs inside a TraceMind-owned work copy;
- a graph becomes *current* only after Graphify succeeded, the graph validated, and the database
  transaction (scan, change record, repository pointer) committed — never partially;
- any failure leaves the previous published graph, snapshot and repository pointer untouched;
- a worker that lost its lease can never publish (fenced completion);
- work directories are removed on success, failure and crash recovery.

Filesystem layout under `data_dir/repos/<repository_id>/`:
    current/             last published snapshot (with graphify-out), swapped in atomically
    current.sha          commit id of `current/`, written after the swap
    graphs/<sha256>.json content-addressed copy of every published graph (queries read these)
    graphs/<sha256>.files.json   the snapshot file list the graph was validated against
    work/<scan>-<worker>-<attempt>/   scratch space for one attempt
    trash/               directories being replaced; emptied after each swap
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracemind.config import Settings
from tracemind.observability.logging import LOGGER_NAME, log
from tracemind.repos.code_graph import CodeGraph, GraphInvalidError
from tracemind.repos.errors import RepoError
from tracemind.repos.git_adapter import FileChange
from tracemind.repos.graphify_runner import GraphifyError, GraphifyRunner
from tracemind.repos.resolver import resolve_release_commit
from tracemind.repos.scan_store import LostLeaseError, ScanOutcome, ScanStore
from tracemind.repos.service import RepositoryService
from tracemind.repos.snapshot import (
    RESERVED_TOP_LEVEL,
    SnapshotLimits,
    apply_changes,
    export_snapshot,
    verify_snapshot,
)
from tracemind.repos.validate import validate_repo_path

_logger = logging.getLogger(LOGGER_NAME + ".scans")

# Failures that retrying cannot fix (configuration, missing data, hard limits).
NON_RETRYABLE = frozenset(
    {
        "not_found",
        "invalid_repository_path",
        "invalid_revision",
        "empty_repository",
        "unsupported_object_format",
        "snapshot_limit_exceeded",
        "graphify_unavailable",
        "graphify_version_mismatch",
        "graphify_install_modified",
        "graph_empty",
        "already_indexed",
        "repository_missing",
    }
)
_REBUILD_NOT_HELPFUL = frozenset(
    {
        "graphify_timeout",
        "graphify_unavailable",
        "graphify_version_mismatch",
        "graphify_install_modified",
    }
)
MAX_FILES_IN_RECORD = 1000


class ScanFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ProcessResult:
    scan_id: str
    status: str  # succeeded | queued (retry scheduled) | failed | abandoned (lease lost)
    error_code: str | None = None
    kind: str | None = None
    rebuilt: bool = False


def classify(exc: BaseException) -> tuple[str, bool]:
    """(stable error code, retryable). Exception text is never used: it may contain paths."""
    if isinstance(exc, ScanFailure):
        return exc.code, exc.retryable
    if isinstance(exc, RepoError):
        return exc.code, exc.code not in NON_RETRYABLE
    if isinstance(exc, GraphifyError):
        return exc.code, exc.code not in NON_RETRYABLE
    if isinstance(exc, GraphInvalidError):
        return "graph_invalid", True
    if isinstance(exc, OSError):
        return "io_error", True
    return "internal_error", True


class _LeaseKeeper:
    """Renews the scan's lease in the background while a scan runs."""

    def __init__(
        self,
        store: ScanStore,
        scan_id: str,
        worker_id: str,
        lease: float,
        clock: Callable[[], datetime],
    ) -> None:
        self._args = (store, scan_id, worker_id, lease, clock)
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        store, scan_id, worker_id, lease, clock = self._args
        while not self._stop.wait(max(0.05, lease / 3)):
            try:
                if not store.heartbeat(
                    scan_id=scan_id, worker_id=worker_id, now=clock(), lease_seconds=lease
                ):
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001 - a transient DB error must not kill the scan
                continue

    def __enter__(self) -> _LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def check(self) -> None:
        if self.lost.is_set():
            raise LostLeaseError


class ScanService:
    def __init__(
        self,
        *,
        repositories: RepositoryService,
        scans: ScanStore,
        runner: GraphifyRunner | None,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        limits: SnapshotLimits | None = None,
    ) -> None:
        self._repos = repositories
        self._scans = scans
        self._runner = runner
        self._settings = settings
        self._clock = clock
        self._limits = limits or SnapshotLimits()
        self._graph_cache: OrderedDict[str, CodeGraph] = OrderedDict()
        self._cache_lock = threading.Lock()

    @property
    def scans(self) -> ScanStore:
        return self._scans

    def _require_runner(self) -> GraphifyRunner:
        if self._runner is None:  # e.g. the API process, which only enqueues and reads
            raise GraphifyError("graphify_unavailable", "not_configured")
        return self._runner

    # ---- paths --------------------------------------------------------------------------

    def _root(self, repository_id: str) -> Path:
        return (self._settings.data_dir / "repos" / repository_id).resolve()

    # ---- enqueue ------------------------------------------------------------------------

    def enqueue(
        self, project_id: str, repository_id: str, revision: object | None = None
    ) -> tuple[dict[str, Any], bool]:
        repo = self._repos.store.get_repository(project_id, repository_id)
        if repo is None:
            raise RepoError("not_found", "repository")
        git = self._repos.open_git(project_id, repository_id)
        if git is None:
            raise RepoError("not_found", "repository")
        wanted = revision if revision is not None else (repo["default_branch"] or "HEAD")
        target = git.resolve_commit(wanted)
        return self._scans.enqueue_scan(
            project_id=project_id,
            repository_id=repository_id,
            target_sha=target,
            max_attempts=self._settings.scan_max_attempts,
            now=self._clock(),
        )

    # ---- worker entry point -------------------------------------------------------------

    def process_next(self, worker_id: str) -> ProcessResult | None:
        scan = self._scans.claim_next(
            worker_id=worker_id, now=self._clock(), lease_seconds=self._settings.scan_lease_seconds
        )
        if scan is None:
            return None
        return self.process(scan, worker_id)

    def process(self, scan: dict[str, Any], worker_id: str) -> ProcessResult:
        started = self._clock()
        work: Path | None = None
        outcome: ScanOutcome | None = None
        keeper = _LeaseKeeper(
            self._scans, scan["scan_id"], worker_id, self._settings.scan_lease_seconds, self._clock
        )
        try:
            with keeper:
                work, outcome, published = self._execute(scan, worker_id, keeper)
                self._scans.complete_success(
                    scan=scan, worker_id=worker_id, now=self._clock(), outcome=outcome
                )
                self._publish(scan["repository_id"], work, scan["target_sha"], published)
                work = None  # moved into place
        except LostLeaseError:
            log(_logger, logging.WARNING, "scan abandoned: lease lost", **_ids(scan, worker_id))
            return ProcessResult(scan["scan_id"], "abandoned", "lease_lost")
        except Exception as exc:  # noqa: BLE001 - classified into a stable code below
            code, retryable = classify(exc)
            try:
                status = self._scans.fail_attempt(
                    scan=scan,
                    worker_id=worker_id,
                    now=self._clock(),
                    error_code=code,
                    retryable=retryable,
                    retry_delay_seconds=self._backoff(scan["attempt"]),
                )
            except LostLeaseError:
                return ProcessResult(scan["scan_id"], "abandoned", "lease_lost")
            log(
                _logger,
                logging.WARNING if status == "queued" else logging.ERROR,
                "scan attempt failed",
                error_code=code,
                error_class=type(exc).__name__,
                resulting_status=status,
                **_ids(scan, worker_id),
            )
            return ProcessResult(scan["scan_id"], status, code)
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            self._sweep_work(scan["repository_id"], scan["scan_id"])
        assert outcome is not None
        log(
            _logger,
            logging.INFO,
            "scan succeeded",
            kind=outcome.kind,
            duration_seconds=round((self._clock() - started).total_seconds(), 3),
            **_ids(scan, worker_id),
        )
        return ProcessResult(
            scan["scan_id"],
            "succeeded",
            None,
            kind=outcome.kind,
            rebuilt=bool(outcome.coverage.get("graph_rebuilt")),
        )

    def _backoff(self, attempt: int) -> float:
        return float(min(300.0, self._settings.scan_backoff_seconds * (2 ** max(0, attempt - 1))))

    # ---- the work -----------------------------------------------------------------------

    def _execute(
        self, scan: dict[str, Any], worker_id: str, keeper: _LeaseKeeper
    ) -> tuple[Path, ScanOutcome, _Published]:
        project_id, repository_id, target = (
            scan["project_id"],
            scan["repository_id"],
            scan["target_sha"],
        )
        repo = self._repos.store.get_repository(project_id, repository_id)
        if repo is None:
            raise ScanFailure("repository_missing")
        git = self._repos.open_git(project_id, repository_id)
        if git is None:
            raise ScanFailure("repository_missing")
        if not git.commit_exists(target):
            raise RepoError("not_found", "revision")
        base: str | None = repo["indexed_sha"]
        if base == target:
            raise ScanFailure("already_indexed")
        keeper.check()

        root = self._root(repository_id)
        work = root / "work" / f"{scan['scan_id']}-{worker_id}-{scan['attempt']}"
        shutil.rmtree(work, ignore_errors=True)
        work.parent.mkdir(parents=True, exist_ok=True)

        previous = self._load_published(repo) if base else None
        incremental = bool(base) and previous is not None and self._can_go_incremental(root, repo)
        kind = "incremental" if incremental else "full"
        self._scans.set_plan(scan_id=scan["scan_id"], worker_id=worker_id, kind=kind, base_sha=base)
        notes: dict[str, Any] = {}

        if incremental:
            assert base is not None
            shutil.copytree(root / "current", work)
            changes = git.diff_files(base, target)
            apply_changes(git, work, changes, self._limits)
            if not verify_snapshot(git, work, target, self._limits).ok:
                # The copied snapshot diverged from Git: rebuild it from the commit itself.
                shutil.rmtree(work, ignore_errors=True)
                export_snapshot(git, target, work, self._limits)
                incremental = False
                kind = "full"
                notes["snapshot_rebuilt"] = True
                self._scans.record_event(
                    scan, event="snapshot_rebuilt", now=self._clock(), worker_id=worker_id
                )
        else:
            export_snapshot(git, target, work, self._limits)
        keeper.check()

        graph, files, rebuilt = self._build_graph(work, incremental)
        if rebuilt:
            notes["graph_rebuilt"] = True
            self._scans.record_event(
                scan, event="graph_rebuilt", now=self._clock(), worker_id=worker_id
            )
        keeper.check()

        coverage = graph.coverage(work, files)
        if coverage.assessed_files > 0 and coverage.files_in_graph == 0:
            raise ScanFailure("graph_empty")  # Graphify "succeeded" but indexed none of the code
        info = self._require_runner().verify()

        changes_for_record = git.diff_files(base, target)  # base=None: every file, as additions
        delta = graph.delta_from(previous).to_json()
        if base is not None and previous is None:
            delta["baseline_missing"] = True  # previous graph unavailable: delta is against nothing
        coverage_json = (
            coverage.to_json() | notes | {"non_code_nodes_dropped": graph.non_code_nodes_dropped}
        )
        commits = git.commits_between(base, target, limit=100)

        published = self._store_graph(root, work, graph, files)
        outcome = ScanOutcome(
            graph_tool="graphify",
            graph_tool_version=info.version,
            graph_schema_version=graph.schema_version,
            graph_sha256=graph.graph_sha256,
            node_count=len(graph.nodes),
            edge_count=len(graph.edges),
            inferred_edge_count=graph.inferred_edge_count,
            files_in_snapshot=coverage.files_in_snapshot,
            files_in_graph=coverage.files_in_graph,
            coverage=coverage_json,
            kind=kind,
            base_sha=base,
            files=_files_json(changes_for_record),
            graph_delta=delta,
            commits=commits,
        )
        keeper.check()
        return work, outcome, published

    def _can_go_incremental(self, root: Path, repo: dict[str, Any]) -> bool:
        marker = root / "current.sha"
        try:
            return (root / "current").is_dir() and marker.read_text(
                encoding="utf-8"
            ).strip() == repo["indexed_sha"]
        except OSError:
            return False

    def _snapshot_files(self, work: Path) -> list[str]:
        out: list[str] = []
        for current, dirs, names in os.walk(work):
            if Path(current) == work:
                dirs[:] = [d for d in dirs if d != RESERVED_TOP_LEVEL]
            out.extend((Path(current) / n).relative_to(work).as_posix() for n in names)
        return sorted(out)

    def _load_graph_file(self, work: Path, files: Sequence[str]) -> CodeGraph:
        return CodeGraph.load(work / "graphify-out" / "graph.json", snapshot_files=files)

    def _build_graph(self, work: Path, incremental: bool) -> tuple[CodeGraph, list[str], bool]:
        files = self._snapshot_files(work)
        try:
            if incremental:
                self._require_runner().update(work)
            else:
                self._require_runner().extract(work)
            return self._load_graph_file(work, files), files, False
        except (GraphifyError, GraphInvalidError) as exc:
            if not incremental or (
                isinstance(exc, GraphifyError) and exc.code in _REBUILD_NOT_HELPFUL
            ):
                raise
        # Recovery: the incremental path produced a failure or an unusable graph. Discard Graphify's
        # output and rebuild from the (already verified) snapshot. The previous published graph is
        # untouched; this only affects the work copy.
        shutil.rmtree(work / "graphify-out", ignore_errors=True)
        self._require_runner().extract(work)
        return self._load_graph_file(work, files), files, True

    # ---- publishing ---------------------------------------------------------------------

    def _store_graph(
        self, root: Path, work: Path, graph: CodeGraph, files: Sequence[str]
    ) -> _Published:
        graphs = root / "graphs"
        graphs.mkdir(parents=True, exist_ok=True)
        target = graphs / f"{graph.graph_sha256}.json"
        meta = graphs / f"{graph.graph_sha256}.files.json"
        if not target.exists():
            _atomic_copy(work / "graphify-out" / "graph.json", target)
        _atomic_write(meta, json.dumps(list(files)))
        return _Published(target, meta)

    def _publish(self, repository_id: str, work: Path, sha: str, published: _Published) -> None:
        root = self._root(repository_id)
        trash = root / "trash" / uuid.uuid4().hex
        trash.parent.mkdir(parents=True, exist_ok=True)
        current = root / "current"
        try:
            if current.exists():
                os.replace(current, trash)
            os.replace(work, current)
            _atomic_write(root / "current.sha", sha)
        except OSError as exc:
            # The database already records the new graph and queries read the content-addressed
            # copy; a stale `current/` is detected on the next scan (current.sha mismatch).
            log(
                _logger,
                logging.ERROR,
                "publish of snapshot failed",
                repository_id=repository_id,
                error_class=type(exc).__name__,
            )
        finally:
            shutil.rmtree(trash, ignore_errors=True)

    def _sweep_work(self, repository_id: str, scan_id: str) -> None:
        work_root = self._root(repository_id) / "work"
        if not work_root.exists():
            return
        for child in work_root.iterdir():
            if child.name.startswith(f"{scan_id}-"):
                shutil.rmtree(child, ignore_errors=True)

    # ---- reading published graphs -------------------------------------------------------

    def _load_published(self, repo: dict[str, Any]) -> CodeGraph | None:
        sha = repo.get("graph_sha256")
        if not sha:
            return None
        with self._cache_lock:
            cached = self._graph_cache.get(sha)
            if cached is not None:
                self._graph_cache.move_to_end(sha)
                return cached
        graphs = self._root(repo["repository_id"]) / "graphs"
        try:
            files = json.loads((graphs / f"{sha}.files.json").read_text(encoding="utf-8"))
            graph = CodeGraph.load(graphs / f"{sha}.json", snapshot_files=files)
        except (OSError, ValueError, GraphInvalidError):
            return None
        if graph.graph_sha256 != sha:  # the content-addressed file was altered
            return None
        with self._cache_lock:
            self._graph_cache[sha] = graph
            while len(self._graph_cache) > 8:
                self._graph_cache.popitem(last=False)
        return graph

    def published_graph(self, project_id: str, repository_id: str) -> CodeGraph | None:
        repo = self._repos.store.get_repository(project_id, repository_id)
        return self._load_published(repo) if repo else None

    # ---- read APIs (always scoped to one project; no filesystem input from callers) -----

    def repository_status(self, project_id: str, repository_id: str) -> dict[str, Any] | None:
        repo = self._repos.store.get_repository(project_id, repository_id)
        if repo is None:
            return None
        latest = self._scans.latest_scan(project_id, repository_id)
        indexed = (
            self._scans.get_scan(project_id, repo["indexed_scan_id"])
            if repo["indexed_scan_id"]
            else None
        )
        if indexed is None:
            state = "never_indexed"
            if latest is not None and latest["status"] in ("queued", "running"):
                state = "never_indexed_scan_pending"
        elif latest is not None and latest["status"] == "failed":
            state = "indexed_last_scan_failed"  # the previous good graph is still being served
        elif latest is not None and latest["status"] in ("queued", "running"):
            state = "indexed_scan_pending"
        else:
            state = "indexed"
        graph: dict[str, Any] | None = None
        if indexed is not None:
            graph = {
                "derived_data": True,
                "note": "Derived by an external tool from the indexed commit; Git and source "
                "remain authoritative.",
                "tool": indexed["graph_tool"],
                "tool_version": indexed["graph_tool_version"],
                "schema_version": indexed["graph_schema_version"],
                "sha256": indexed["graph_sha256"],
                "node_count": indexed["node_count"],
                "edge_count": indexed["edge_count"],
                "inferred_edge_count": indexed["inferred_edge_count"],
                "coverage": indexed["coverage"],
            }
        return {
            "repository": repo,
            "index_state": state,
            "indexed_sha": repo["indexed_sha"],
            "graph": graph,
            "last_scan": latest,
            "last_successful_scan": indexed,
        }

    def changes(
        self,
        project_id: str,
        repository_id: str,
        *,
        limit: int,
        cursor: str | None,
        commit_sha: object | None = None,
        release: str | None = None,
    ) -> dict[str, Any] | None:
        repo = self._repos.store.get_repository(project_id, repository_id)
        if repo is None:
            return None
        mapping: dict[str, Any] | None = None
        contains: str | None = None
        if commit_sha is not None or release is not None:
            try:
                git = self._repos.open_git(project_id, repository_id)
            except RepoError:
                git = None
            resolution = resolve_release_commit(git, release=release, commit_sha=commit_sha)
            mapping = resolution.to_json()
            if resolution.confidence != "verified":
                return {"items": [], "next_cursor": None, "release_mapping": mapping}
            contains = resolution.commit_sha
        items, nxt = self._scans.list_change_records(
            project_id, repository_id, limit=limit, cursor=cursor, contains_commit=contains
        )
        shas = sorted({s for item in items for s in item["commits"]})
        details = self._scans.commit_details(repository_id, shas)
        for item in items:
            item["commits"] = [details.get(s, {"sha": s}) for s in item["commits"]]
        out: dict[str, Any] = {"items": items, "next_cursor": nxt}
        if mapping is not None:
            out["release_mapping"] = mapping
        return out

    def code_context(
        self,
        project_id: str,
        repository_id: str,
        *,
        file: object | None = None,
        symbol: object | None = None,
    ) -> dict[str, Any]:
        """Bounded code relationships for ONE file or ONE symbol name, from the published graph.

        The caller's string is only ever used as a dictionary key into the already-loaded graph:
        it is never turned into a filesystem path, git argument or pattern."""
        if (file is None) == (symbol is None):
            raise RepoError("invalid_query", "exactly_one_of_file_or_symbol")
        # Validate the caller's input before anything is loaded: bad input never reaches storage.
        clean_file: str | None = None
        clean_symbol: str | None = None
        if file is not None:
            if not isinstance(file, str):
                raise RepoError("invalid_path", "empty_or_not_text")
            clean_file = validate_repo_path(file)
        else:
            if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 200:
                raise RepoError("invalid_query", "symbol")
            clean_symbol = symbol
        repo = self._repos.store.get_repository(project_id, repository_id)
        if repo is None:
            raise RepoError("not_found", "repository")
        graph = self._load_published(repo)
        if graph is None or not repo["indexed_sha"]:
            raise RepoError("not_indexed")
        indexed = (
            self._scans.get_scan(project_id, repo["indexed_scan_id"])
            if repo["indexed_scan_id"]
            else None
        )
        coverage = (indexed or {}).get("coverage") or {}
        provenance = {
            "derived_data": True,
            "tool": "graphify",
            "tool_version": graph.graphify_version,
            "graph_sha256": graph.graph_sha256,
            "indexed_sha": repo["indexed_sha"],
            "coverage_complete": bool(coverage.get("complete")),
            "confidence_legend": {
                "EXTRACTED": "found directly in the parsed source",
                "INFERRED": "guessed by the tool; not a fact",
                "UNKNOWN": "confidence not reported; treat as not verified",
            },
        }
        if clean_file is not None:
            ctx = graph.file_context(clean_file)
            if ctx is None:
                raise RepoError("not_in_graph", "file")
            warnings = []
            if clean_file in coverage.get("python_syntax_errors", []):
                warnings.append("python_syntax_error_in_file: indexing of this file is incomplete")
            return {"provenance": provenance, "file_context": ctx, "warnings": warnings}
        assert clean_symbol is not None
        matches = graph.find_symbols(clean_symbol, limit=10)
        if not matches:
            raise RepoError("not_in_graph", "symbol")
        return {
            "provenance": provenance,
            "symbols": [graph.symbol_context(node) for node in matches],
            "warnings": [],
        }


@dataclass(frozen=True)
class _Published:
    graph_file: Path
    files_file: Path


def _ids(scan: dict[str, Any], worker_id: str) -> dict[str, Any]:
    return {
        "scan_id": scan["scan_id"],
        "repository_id": scan["repository_id"],
        "project_id": scan["project_id"],
        "attempt": scan["attempt"],
        "worker_id": worker_id,
    }


def _files_json(changes: Sequence[FileChange]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for c in changes:
        counts[c.status] = counts.get(c.status, 0) + 1
    items = [
        {
            "path": c.path,
            "status": c.status,
            "old_path": c.old_path,
            "additions": c.additions,
            "deletions": c.deletions,
            "binary": c.binary,
        }
        for c in changes[:MAX_FILES_IN_RECORD]
    ]
    return {
        "items": items,
        "total": len(changes),
        "truncated": len(changes) > MAX_FILES_IN_RECORD,
        "counts": counts,
    }


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
