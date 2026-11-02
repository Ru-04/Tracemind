"""TraceMind's internal view of a code graph, loaded from Graphify's `graph.json`.

Graphify output is **derived data**. This module validates it before anything uses it, maps it
onto the snapshot it was built from, and exposes a small, bounded set of queries. Every result
carries `confidence` (EXTRACTED / INFERRED / UNKNOWN), file and line. An absent edge is not
evidence that two things are independent, and an INFERRED edge is not a fact.

Other code depends on this interface, not on Graphify's file format, so a fallback indexer
(ADR-0003) could replace Graphify without touching callers.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

SUPPORTED_SCHEMA_VERSION = 1
MAX_GRAPH_BYTES = 64 * 1024 * 1024
MAX_NODES = 500_000
MAX_EDGES = 2_000_000
QUERY_LIMIT = 100
CALL_RELATIONS = frozenset({"calls", "indirect_call"})
# Languages whose extraction was actually verified in the M2 spike. Coverage claims stop here.
VERIFIED_EXTENSIONS: Mapping[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


class GraphInvalidError(Exception):
    """The graph file cannot be trusted. `code` is stable; `detail` never includes content."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"graph_invalid:{detail}")
        self.code = "graph_invalid"
        self.detail = detail


@dataclass(frozen=True)
class GraphNode:
    id: str
    label: str
    file: str  # snapshot-relative, forward slashes
    line: int | None
    kind: str  # file | class | function | other — inferred from Graphify's fields


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    confidence: str  # EXTRACTED | INFERRED | UNKNOWN
    confidence_score: float | None
    file: str | None
    line: int | None


@dataclass
class Coverage:
    """What the graph does and does not tell us about the snapshot."""

    files_in_snapshot: int
    assessed_files: int  # files in a language whose extraction was verified (python, javascript)
    files_in_graph: int  # assessed files that have at least one node
    not_assessed_files: int  # other languages: no claim is made
    assessed_without_nodes: list[str] = field(default_factory=list)
    python_syntax_errors: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "files_in_snapshot": self.files_in_snapshot,
            "assessed_files": self.assessed_files,
            "files_in_graph": self.files_in_graph,
            "not_assessed_files": self.not_assessed_files,
            "assessed_without_nodes": self.assessed_without_nodes[:50],
            "python_syntax_errors": self.python_syntax_errors[:50],
            "languages": self.languages,
            "complete": not self.assessed_without_nodes and not self.python_syntax_errors,
        }


@dataclass(frozen=True)
class GraphDelta:
    nodes_added: int
    nodes_removed: int
    edges_added: int
    edges_removed: int
    added_symbols: list[str]
    removed_symbols: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "nodes_added": self.nodes_added,
            "nodes_removed": self.nodes_removed,
            "edges_added": self.edges_added,
            "edges_removed": self.edges_removed,
            "added_symbols": self.added_symbols,
            "removed_symbols": self.removed_symbols,
        }


def _norm_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("/")


def _line(value: Any) -> int | None:
    if isinstance(value, str) and value[:1] == "L" and value[1:].split("-")[0].isdigit():
        return int(value[1:].split("-")[0])
    return None


def _kind(raw: Mapping[str, Any], file: str) -> str:
    label = str(raw.get("label", ""))
    if raw.get("_callable_class"):
        return "class"
    if label == PurePosixPath(file).name:
        return "file"
    if label.endswith("()"):
        return "function"
    return "other"


class CodeGraph:
    def __init__(
        self,
        nodes: dict[str, GraphNode],
        edges: list[GraphEdge],
        *,
        graph_sha256: str,
        graphify_version: str,
        schema_version: int,
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.graph_sha256 = graph_sha256
        self.graphify_version = graphify_version
        self.schema_version = schema_version
        self.non_code_nodes_dropped = 0
        self._out: dict[str, list[GraphEdge]] = {}
        self._in: dict[str, list[GraphEdge]] = {}
        for edge in edges:
            self._out.setdefault(edge.source, []).append(edge)
            self._in.setdefault(edge.target, []).append(edge)

    # ---- loading and validation ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path, *, snapshot_files: Collection[str]) -> CodeGraph:
        """Validate and load `graph.json`. Raises GraphInvalidError for anything untrustworthy:
        unreadable or oversized file, wrong schema version, duplicate nodes, edges that point at
        missing nodes (a sign of a partial write), or nodes that cannot be mapped to a file of
        the snapshot."""
        try:
            size = path.stat().st_size
        except OSError:
            raise GraphInvalidError("missing") from None
        if size > MAX_GRAPH_BYTES:
            raise GraphInvalidError("too_large")
        raw_bytes = path.read_bytes()
        try:
            doc = json.loads(raw_bytes)
        except (ValueError, RecursionError):
            raise GraphInvalidError("not_json") from None
        if not isinstance(doc, dict):
            raise GraphInvalidError("not_an_object")
        meta = doc.get("graph")
        if not isinstance(meta, dict) or not isinstance(meta.get("graphify_version"), str):
            raise GraphInvalidError("missing_metadata")
        if meta.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            raise GraphInvalidError("unsupported_schema_version")
        raw_nodes, raw_edges = doc.get("nodes"), doc.get("links")
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise GraphInvalidError("missing_nodes_or_links")
        if len(raw_nodes) > MAX_NODES or len(raw_edges) > MAX_EDGES:
            raise GraphInvalidError("too_many_elements")

        known = {f: f for f in snapshot_files}
        nodes: dict[str, GraphNode] = {}
        non_code: set[str] = set()
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                raise GraphInvalidError("node_not_an_object")
            node_id, label, source = raw.get("id"), raw.get("label"), raw.get("source_file")
            if not (
                isinstance(node_id, str) and isinstance(label, str) and isinstance(source, str)
            ):
                raise GraphInvalidError("node_missing_fields")
            if node_id in nodes or node_id in non_code:
                raise GraphInvalidError("duplicate_node_id")
            if raw.get("file_type") != "code":
                # `update` adds document nodes (e.g. README.md) that `extract --code-only` omits.
                # Only code is modelled, so both paths yield the same graph for the same commit.
                non_code.add(node_id)
                continue
            file = _map_to_snapshot(_norm_path(source), known)
            if file is None:
                raise GraphInvalidError("node_not_in_snapshot")
            nodes[node_id] = GraphNode(
                node_id, label, file, _line(raw.get("source_location")), _kind(raw, file)
            )

        edges: list[GraphEdge] = []
        for raw in raw_edges:
            if not isinstance(raw, dict):
                raise GraphInvalidError("edge_not_an_object")
            src, dst, relation = raw.get("source"), raw.get("target"), raw.get("relation")
            if not (isinstance(src, str) and isinstance(dst, str) and isinstance(relation, str)):
                raise GraphInvalidError("edge_missing_fields")
            if src in non_code or dst in non_code:
                continue
            if src not in nodes or dst not in nodes:
                raise GraphInvalidError("edge_references_missing_node")
            confidence = raw.get("confidence")
            score = raw.get("confidence_score")
            edges.append(
                GraphEdge(
                    src,
                    dst,
                    relation,
                    confidence if confidence in ("EXTRACTED", "INFERRED") else "UNKNOWN",
                    float(score)
                    if isinstance(score, int | float) and not isinstance(score, bool)
                    else None,
                    _map_to_snapshot(_norm_path(raw["source_file"]), known)
                    if isinstance(raw.get("source_file"), str)
                    else None,
                    _line(raw.get("source_location")),
                )
            )
        graph = cls(
            nodes,
            edges,
            graph_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            graphify_version=str(meta["graphify_version"]),
            schema_version=SUPPORTED_SCHEMA_VERSION,
        )
        graph.non_code_nodes_dropped = len(non_code)
        return graph

    # ---- summaries ----------------------------------------------------------------------

    @property
    def inferred_edge_count(self) -> int:
        return sum(1 for e in self.edges if e.confidence != "EXTRACTED")

    def files(self) -> set[str]:
        return {n.file for n in self.nodes.values()}

    def coverage(self, snapshot_dir: Path, snapshot_files: Collection[str]) -> Coverage:
        """Compare the graph with what the snapshot actually contains. For Python files the
        snapshot is also parsed (never executed) to find syntax errors, which Graphify indexes
        silently."""
        in_graph = self.files()
        languages: Counter[str] = Counter()
        assessed: list[str] = []
        for f in snapshot_files:
            lang = VERIFIED_EXTENSIONS.get(PurePosixPath(f).suffix.lower())
            if lang:
                languages[lang] += 1
                assessed.append(f)
        without = sorted(f for f in assessed if f not in in_graph)
        syntax_errors: list[str] = []
        for f in sorted(assessed):
            if f.endswith(".py") and _has_python_syntax_error(snapshot_dir / f):
                syntax_errors.append(f)
        return Coverage(
            files_in_snapshot=len(snapshot_files),
            assessed_files=len(assessed),
            files_in_graph=len([f for f in assessed if f in in_graph]),
            not_assessed_files=len(snapshot_files) - len(assessed),
            assessed_without_nodes=without,
            python_syntax_errors=syntax_errors,
            languages=dict(languages),
        )

    def delta_from(self, previous: CodeGraph | None) -> GraphDelta:
        old_nodes = set(previous.nodes) if previous else set()
        new_nodes = set(self.nodes)
        old_edges = {_edge_key(e) for e in previous.edges} if previous else set()
        new_edges = {_edge_key(e) for e in self.edges}
        added = sorted(
            self._describe(i) for i in new_nodes - old_nodes if self.nodes[i].kind != "file"
        )
        removed = (
            sorted(
                previous._describe(i)
                for i in old_nodes - new_nodes
                if previous.nodes[i].kind != "file"
            )
            if previous
            else []
        )
        return GraphDelta(
            nodes_added=len(new_nodes - old_nodes),
            nodes_removed=len(old_nodes - new_nodes),
            edges_added=len(new_edges - old_edges),
            edges_removed=len(old_edges - new_edges),
            added_symbols=added[:200],
            removed_symbols=removed[:200],
        )

    def _describe(self, node_id: str) -> str:
        n = self.nodes[node_id]
        return f"{n.file}:{n.label}"

    # ---- bounded queries (the only ways callers may look into the graph) ----------------

    def find_symbols(self, name: str, *, limit: int = 20) -> list[GraphNode]:
        """Symbols whose label equals `name` (ignoring a leading '.' and trailing '()')."""
        wanted = name.strip().lstrip(".").removesuffix("()")
        if not wanted or len(wanted) > 200:
            return []
        hits = [
            n
            for n in self.nodes.values()
            if n.kind != "file" and n.label.lstrip(".").removesuffix("()") == wanted
        ]
        return sorted(hits, key=lambda n: (n.file, n.line or 0))[: min(limit, QUERY_LIMIT)]

    def file_context(self, file: str) -> dict[str, Any] | None:
        """Symbols defined in `file` and how it connects to other files. None if the file has no
        nodes in this graph (the caller must not read that as 'the file does not matter')."""
        mine = [n for n in self.nodes.values() if n.file == file]
        if not mine:
            return None
        mine_ids = {n.id for n in mine}
        imports: list[dict[str, Any]] = []
        imported_by: list[dict[str, Any]] = []
        calls_out: list[dict[str, Any]] = []
        calls_in: list[dict[str, Any]] = []
        for nid in mine_ids:
            for e in self._out.get(nid, []):
                other = self.nodes[e.target]
                if other.file == file:
                    continue
                row = self._edge_row(e, other)
                if e.relation == "imports_from":
                    imports.append(row)
                elif e.relation in CALL_RELATIONS:
                    calls_out.append({**row, "from": self.nodes[e.source].label})
            for e in self._in.get(nid, []):
                other = self.nodes[e.source]
                if other.file == file:
                    continue
                row = self._edge_row(e, other)
                if e.relation == "imports_from":
                    imported_by.append(row)
                elif e.relation in CALL_RELATIONS:
                    calls_in.append({**row, "to": self.nodes[e.target].label})
        return {
            "file": file,
            "symbols": [
                {"label": n.label, "kind": n.kind, "line": n.line}
                for n in sorted(mine, key=lambda n: (n.line or 0, n.label))
                if n.kind != "file"
            ][:QUERY_LIMIT],
            "imports": _cap(imports),
            "imported_by": _cap(imported_by),
            "calls_out": _cap(calls_out),
            "calls_in": _cap(calls_in),
        }

    def symbol_context(self, node: GraphNode) -> dict[str, Any]:
        """Direct callers and callees of one symbol (depth 1)."""
        callers = [
            {**self._edge_row(e, self.nodes[e.source])}
            for e in self._in.get(node.id, [])
            if e.relation in CALL_RELATIONS
        ]
        callees = [
            {**self._edge_row(e, self.nodes[e.target])}
            for e in self._out.get(node.id, [])
            if e.relation in CALL_RELATIONS
        ]
        return {
            "symbol": {
                "label": node.label,
                "kind": node.kind,
                "file": node.file,
                "line": node.line,
            },
            "callers": _cap(callers),
            "callees": _cap(callees),
        }

    @staticmethod
    def _edge_row(edge: GraphEdge, other: GraphNode) -> dict[str, Any]:
        return {
            "label": other.label,
            "file": other.file,
            "line": other.line,
            "relation": edge.relation,
            "confidence": edge.confidence,
            "confidence_score": edge.confidence_score,
            "evidence_file": edge.file,
            "evidence_line": edge.line,
        }


def _cap(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: (r["file"], r["line"] or 0, r["label"], r["relation"]))
    return {"items": rows[:QUERY_LIMIT], "truncated": len(rows) > QUERY_LIMIT, "total": len(rows)}


def _edge_key(e: GraphEdge) -> tuple[str, str, str]:
    return (e.source, e.relation, e.target)


def _map_to_snapshot(path: str, known: Mapping[str, str]) -> str | None:
    """Graphify records paths relative to its working directory. Accept the path as given, or
    with leading components stripped, but only if it names a real file of the snapshot."""
    parts = path.split("/")
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        if candidate in known:
            return candidate
    return None


def _has_python_syntax_error(path: Path) -> bool:
    try:
        source = path.read_bytes()
    except OSError:
        return False
    try:
        ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True
    return False
