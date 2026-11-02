"""CodeGraph: validation of (possibly corrupt or incomplete) Graphify output, honest coverage, and
bounded queries. Uses a golden graph.json produced by the real pinned Graphify 0.9.97."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tracemind.repos import code_graph as cg
from tracemind.repos.code_graph import CodeGraph, GraphInvalidError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "graphify"
GOLDEN: dict[str, Any] = json.loads((FIXTURES / "graph-0.9.97.json").read_text(encoding="utf-8"))
FILES: list[str] = json.loads((FIXTURES / "snapshot-files.json").read_text(encoding="utf-8"))


def write(tmp_path: Path, doc: Any, name: str = "graph.json") -> Path:
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def golden_path(tmp_path: Path) -> Path:
    return write(tmp_path, GOLDEN)


@pytest.fixture
def graph(golden_path: Path) -> CodeGraph:
    return CodeGraph.load(golden_path, snapshot_files=FILES)


def mutated(fn: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    doc = copy.deepcopy(GOLDEN)
    fn(doc)
    return doc


def test_golden_graph_loads_with_provenance(graph: CodeGraph, golden_path: Path) -> None:
    import hashlib

    assert (len(graph.nodes), len(graph.edges)) == (13, 19)
    assert graph.graphify_version == "0.9.97" and graph.schema_version == 1
    assert graph.graph_sha256 == hashlib.sha256(golden_path.read_bytes()).hexdigest()
    assert graph.files() == {f for f in FILES if f != "README.md"}
    assert graph.inferred_edge_count == 0


# ---- corrupted / incomplete output must never load ---------------------------------------

INVALID: dict[str, tuple[Callable[[Path], Path], str]] = {
    "truncated json": (lambda t: write(t, '{"nodes": ['), "not_json"),
    "empty file": (lambda t: write(t, ""), "not_json"),
    "json array": (lambda t: write(t, "[]"), "not_an_object"),
    "no metadata": (lambda t: write(t, mutated(lambda d: d.pop("graph"))), "missing_metadata"),
    "wrong schema": (
        lambda t: write(t, mutated(lambda d: d["graph"].update(schema_version=2))),
        "unsupported_schema_version",
    ),
    "no nodes": (lambda t: write(t, mutated(lambda d: d.pop("nodes"))), "missing_nodes_or_links"),
    "no links": (lambda t: write(t, mutated(lambda d: d.pop("links"))), "missing_nodes_or_links"),
    "node not object": (
        lambda t: write(t, mutated(lambda d: d["nodes"].append(5))),
        "node_not_an_object",
    ),
    "node without id": (
        lambda t: write(t, mutated(lambda d: d["nodes"][0].pop("id"))),
        "node_missing_fields",
    ),
    "node without file": (
        lambda t: write(t, mutated(lambda d: d["nodes"][0].pop("source_file"))),
        "node_missing_fields",
    ),
    "duplicate node id": (
        lambda t: write(t, mutated(lambda d: d["nodes"].append(dict(d["nodes"][0])))),
        "duplicate_node_id",
    ),
    "node outside snapshot": (
        lambda t: write(
            t, mutated(lambda d: d["nodes"][0].update(source_file="not/in/snapshot.py"))
        ),
        "node_not_in_snapshot",
    ),
    "edge to missing node (partial write)": (
        lambda t: write(t, mutated(lambda d: d["nodes"].pop(0))),
        "edge_references_missing_node",
    ),
    "edge not object": (
        lambda t: write(t, mutated(lambda d: d["links"].append("x"))),
        "edge_not_an_object",
    ),
    "edge without relation": (
        lambda t: write(t, mutated(lambda d: d["links"][0].pop("relation"))),
        "edge_missing_fields",
    ),
    "missing file": (lambda t: t / "nope.json", "missing"),
}


@pytest.mark.parametrize("name", list(INVALID), ids=list(INVALID))
def test_corrupt_or_incomplete_graphs_are_rejected(name: str, tmp_path: Path) -> None:
    make, detail = INVALID[name]
    path = make(tmp_path)
    with pytest.raises(GraphInvalidError) as info:
        CodeGraph.load(path, snapshot_files=FILES)
    assert info.value.code == "graph_invalid" and info.value.detail == detail


def test_oversized_graph_is_rejected(golden_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cg, "MAX_GRAPH_BYTES", 100)
    with pytest.raises(GraphInvalidError) as info:
        CodeGraph.load(golden_path, snapshot_files=FILES)
    assert info.value.detail == "too_large"


def test_element_count_limits(golden_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cg, "MAX_NODES", 5)
    with pytest.raises(GraphInvalidError) as info:
        CodeGraph.load(golden_path, snapshot_files=FILES)
    assert info.value.detail == "too_many_elements"


def test_errors_never_contain_graph_content(tmp_path: Path) -> None:
    doc = mutated(lambda d: d["nodes"][0].update(source_file="SECRET/path/token.py"))
    with pytest.raises(GraphInvalidError) as info:
        CodeGraph.load(write(tmp_path, doc), snapshot_files=FILES)
    assert "SECRET" not in str(info.value)


def test_unknown_confidence_is_labelled_not_trusted(tmp_path: Path) -> None:
    doc = mutated(lambda d: d["links"][0].update(confidence="WEIRD", confidence_score=True))
    g = CodeGraph.load(write(tmp_path, doc), snapshot_files=FILES)
    assert g.edges[0].confidence == "UNKNOWN" and g.edges[0].confidence_score is None
    assert g.inferred_edge_count == 1  # anything not EXTRACTED counts as "not a fact"


def test_inferred_edges_stay_labelled(tmp_path: Path) -> None:
    doc = mutated(lambda d: d["links"][0].update(confidence="INFERRED", confidence_score=0.4))
    g = CodeGraph.load(write(tmp_path, doc), snapshot_files=FILES)
    assert g.edges[0].confidence == "INFERRED" and g.edges[0].confidence_score == 0.4
    ctx = g.symbol_context(g.nodes[g.edges[0].target])
    labels = {row["confidence"] for key in ("callers", "callees") for row in ctx[key]["items"]}
    assert labels <= {"EXTRACTED", "INFERRED"}


# ---- path normalization ------------------------------------------------------------------


def test_paths_relative_to_the_wrong_directory_are_mapped_onto_the_snapshot(tmp_path: Path) -> None:
    def prefix(d: dict[str, Any]) -> None:
        for n in d["nodes"]:
            n["source_file"] = "probe\\" + n["source_file"].replace("/", "\\")
        for e in d["links"]:
            e["source_file"] = "probe/" + e["source_file"]

    g = CodeGraph.load(write(tmp_path, mutated(prefix)), snapshot_files=FILES)
    assert g.files() == {f for f in FILES if f != "README.md"}


# ---- queries -----------------------------------------------------------------------------


def test_symbol_callers_and_callees_carry_confidence_and_location(graph: CodeGraph) -> None:
    (target,) = graph.find_symbols("average_item_price")
    assert (target.file, target.line, target.kind) == ("app/pricing.py", 1, "function")
    ctx = graph.symbol_context(target)
    callers = {(r["file"], r["label"]) for r in ctx["callers"]["items"]}
    assert callers == {("app/orders.py", ".avg()"), ("app/pricing.py", "regression()")}
    for row in ctx["callers"]["items"]:
        assert row["confidence"] == "EXTRACTED" and row["evidence_file"] and row["evidence_line"]
    assert ctx["callees"]["items"] == [] and not ctx["callers"]["truncated"]


def test_class_level_resolution_is_visible_not_hidden(graph: CodeGraph) -> None:
    """handler() calls OrderService().avg(); the graph links handler to the *class*, not .avg().
    That imprecision must stay visible to callers instead of being papered over."""
    (handler,) = graph.find_symbols("handler")
    callees = {r["label"] for r in graph.symbol_context(handler)["callees"]["items"]}
    assert "OrderService" in callees and ".avg()" not in callees


def test_file_context_shows_imports_and_calls(graph: CodeGraph) -> None:
    ctx = graph.file_context("app/orders.py")
    assert ctx is not None
    assert {r["file"] for r in ctx["imports"]["items"]} == {"app/pricing.py"}
    assert {r["file"] for r in ctx["imported_by"]["items"]} == {"app/main.py"}
    assert {s["label"] for s in ctx["symbols"]} >= {"OrderService", ".avg()", ".total()"}
    assert any(r["file"] == "app/pricing.py" for r in ctx["calls_out"]["items"])
    assert graph.file_context("README.md") is None  # absent from the graph != irrelevant
    assert graph.file_context("../../etc/passwd") is None


def test_unknown_and_hostile_symbol_names_find_nothing(graph: CodeGraph) -> None:
    assert graph.find_symbols("does_not_exist") == []
    assert graph.find_symbols("") == []
    assert graph.find_symbols("x" * 1000) == []
    assert graph.find_symbols("*") == [] and graph.find_symbols(".*") == []  # no pattern matching


def test_query_results_are_bounded() -> None:
    nodes = {f"n{i}": cg.GraphNode(f"n{i}", f"f{i}()", "a.py", i, "function") for i in range(300)}
    nodes["hub"] = cg.GraphNode("hub", "hub()", "b.py", 1, "function")
    edges = [cg.GraphEdge(f"n{i}", "hub", "calls", "EXTRACTED", 1.0, "a.py", i) for i in range(300)]
    big = CodeGraph(nodes, edges, graph_sha256="x", graphify_version="0.9.97", schema_version=1)
    ctx = big.symbol_context(nodes["hub"])
    assert len(ctx["callers"]["items"]) == cg.QUERY_LIMIT
    assert ctx["callers"]["truncated"] and ctx["callers"]["total"] == 300
    assert len(big.find_symbols("f1", limit=10_000)) <= cg.QUERY_LIMIT


# ---- coverage honesty --------------------------------------------------------------------


def make_snapshot(tmp_path: Path, files: dict[str, str]) -> tuple[Path, list[str]]:
    root = tmp_path / "snap"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root, sorted(files)


def test_coverage_is_complete_when_every_verified_file_has_nodes(
    graph: CodeGraph, tmp_path: Path
) -> None:
    root, names = make_snapshot(
        tmp_path, {f: "x = 1\n" if f.endswith(".py") else "// js\n" for f in FILES}
    )
    cov = graph.coverage(root, names)
    assert cov.assessed_files == cov.files_in_graph == 5 and cov.not_assessed_files == 1
    assert cov.to_json()["complete"] is True
    assert cov.languages == {"python": 3, "javascript": 2}


def test_coverage_flags_files_without_nodes_and_unverified_languages(
    graph: CodeGraph, tmp_path: Path
) -> None:
    files = {f: "x = 1\n" for f in FILES if f.endswith(".py")} | {
        "web/ui.js": "//",
        "web/util.js": "//",
    }
    files |= {"app/extra.py": "y = 2\n", "svc/main.go": "package main\n", "README.md": "d\n"}
    root, names = make_snapshot(tmp_path, files)
    cov = graph.coverage(root, names)
    assert cov.assessed_without_nodes == ["app/extra.py"]
    assert cov.not_assessed_files == 2  # the Go file and README: no claim is made about them
    assert "go" not in cov.languages
    assert cov.to_json()["complete"] is False


def test_python_syntax_errors_are_reported_even_though_graphify_is_silent(
    graph: CodeGraph, tmp_path: Path
) -> None:
    files = {f: "x = 1\n" for f in FILES if f.endswith(".py")}
    files["app/pricing.py"] = "def broken(:\n    pass\n"
    files |= {"web/ui.js": "//", "web/util.js": "//", "README.md": "d"}
    root, names = make_snapshot(tmp_path, files)
    cov = graph.coverage(root, names)
    assert cov.python_syntax_errors == ["app/pricing.py"]
    assert cov.to_json()["complete"] is False


def test_parsing_never_executes_snapshot_code(graph: CodeGraph, tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    files = {f: "x = 1\n" for f in FILES if f.endswith(".py")}
    files["app/main.py"] = f"open({str(marker)!r}, 'w').write('ran')\n"
    files |= {"web/ui.js": "//", "web/util.js": "//", "README.md": "d"}
    root, names = make_snapshot(tmp_path, files)
    graph.coverage(root, names)
    assert not marker.exists()


# ---- deltas ------------------------------------------------------------------------------


def test_delta_between_graph_versions(graph: CodeGraph, tmp_path: Path) -> None:
    def drop_ui(d: dict[str, Any]) -> None:
        gone = {n["id"] for n in d["nodes"] if n["source_file"] == "web/ui.js"}
        d["nodes"] = [n for n in d["nodes"] if n["id"] not in gone]
        d["links"] = [e for e in d["links"] if e["source"] not in gone and e["target"] not in gone]

    older = CodeGraph.load(write(tmp_path, mutated(drop_ui), "old.json"), snapshot_files=FILES)
    delta = graph.delta_from(older)
    assert (delta.nodes_added, delta.nodes_removed) == (2, 0)
    assert delta.added_symbols == ["web/ui.js:render()"] and delta.removed_symbols == []
    reverse = older.delta_from(graph)
    assert reverse.nodes_removed == 2 and reverse.removed_symbols == ["web/ui.js:render()"]
    first = graph.delta_from(None)
    assert first.nodes_added == 13 and first.nodes_removed == 0
    assert graph.delta_from(graph).nodes_added == 0


def test_document_nodes_added_by_update_are_dropped_not_treated_as_corruption(
    tmp_path: Path,
) -> None:
    def add_docs(d: dict[str, Any]) -> None:
        d["nodes"] += [
            {
                "id": "readme",
                "label": "README.md",
                "source_file": "README.md",
                "file_type": "document",
            },
            {
                "id": "readme_title",
                "label": "Title",
                "source_file": "README.md",
                "file_type": "document",
            },
        ]
        d["links"] += [{"source": "readme", "target": "readme_title", "relation": "contains"}]
        d["links"] += [
            {"source": "readme_title", "target": d["nodes"][0]["id"], "relation": "references"}
        ]

    g = CodeGraph.load(write(tmp_path, mutated(add_docs)), snapshot_files=FILES)
    assert (len(g.nodes), len(g.edges), g.non_code_nodes_dropped) == (13, 19, 2)
    assert "README.md" not in g.files()


def test_a_dangling_edge_is_still_invalid_when_documents_are_present(tmp_path: Path) -> None:
    def bad(d: dict[str, Any]) -> None:
        d["nodes"].append(
            {"id": "doc", "label": "x", "source_file": "README.md", "file_type": "document"}
        )
        d["links"].append({"source": "doc", "target": "ghost", "relation": "contains"})
        d["links"].append({"source": "ghost", "target": d["nodes"][0]["id"], "relation": "calls"})

    with pytest.raises(GraphInvalidError) as info:
        CodeGraph.load(write(tmp_path, mutated(bad)), snapshot_files=FILES)
    assert info.value.detail == "edge_references_missing_node"
