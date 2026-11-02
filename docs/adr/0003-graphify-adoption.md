# ADR-0003: Graphify for code relationships — adopt as an isolated, pinned CLI

- Status: **ACCEPTED** by the project owner on 2026-10-28 (decision below as recommended; constraints 1–8 are binding)
- Milestone: M2
- Related: SOW §8.3 (Graphify verification gate), ADR-0001

## Context

SOW §8.3 requires Graphify's actual capabilities to be verified before integration and forbids assuming any command, API or format. Git stays the authority for commits and diffs; Graphify output is derived data that must never override Git or source code.

## What was verified (2026-10-28, Windows 11, Python 3.12.10)

Method: installed `graphifyy` into an **isolated virtual environment outside the project**, then ran its CLI against throwaway Git repositories (a 5-file fixture and a copy of this project's own 47 Python files), with all LLM API keys unset and `HTTP(S)_PROXY` pointed at a dead port so any network attempt would fail visibly. Full evidence: [docs/dependency-verification.md](../dependency-verification.md#graphify--spike-results-2026-10-28).

| Question (SOW §8.3) | Verified finding |
|---|---|
| Which tool | PyPI package `graphifyy` (double y), CLI `graphify`, version 0.9.97. Package metadata names `github.com/Graphify-Labs/graphify` as its repository. License Apache-2.0 (plus MIT file and NOTICE) |
| Languages | Python and JavaScript were extracted and checked. Tree-sitter grammars for ~25 other languages (TypeScript, Go, Java, Rust, C/C++, C#, Ruby, PHP, Kotlin, Swift, …) install with it but were **not exercised** |
| Install / runtime | Python ≥ 3.10. Base install pulls 31 packages (numpy, networkx, tree-sitter and grammars). No compiler needed on Windows; pure wheel. Extras (LLM backends, PDF, video, Neo4j, …) are optional and not needed |
| Headless interface | `graphify extract <dir> --code-only` (local AST extraction, no API key) for the first scan, and `graphify update <dir>` (documented "no LLM needed") for incremental updates. Exit code 0 on success, 1 on failure. No documented stable Python API was found (`import graphify` exposes no public names); the CLI is the interface |
| LLM / network | With keys unset and a dead proxy, `extract --code-only` and `update` succeeded. Network-capable modules exist (`llm.py` for semantic extraction, `security.py`), used only by non-`--code-only` extraction and `add <url>`. Not proven by traffic capture; mitigated by scrubbing API keys from the child environment and never invoking those commands |
| Output | `graphify-out/graph.json` (NetworkX node-link JSON): `directed`, `multigraph`, `graph` (`schema_version: 1`, `graphify_version`), `nodes`, `links`, `hyperedges`, `built_at_commit`. Nodes carry `id`, `label`, `source_file`, `source_location` (`L<line>`), `file_type`, `community`. Edges carry `relation` (calls, imports, imports_from, contains, method, inherits, references, uses, …), `confidence` (`EXTRACTED` or `INFERRED`), `confidence_score`, `source_file`, `source_location` |
| Persistent files | Writes `graphify-out/` **inside the scanned directory**: `graph.json`, `graph.html`, `GRAPH_REPORT.md`, `manifest.json` (mtimes + content hashes), `cache/`, and `.graphify_root` containing an **absolute local path**. |
| Updates | Incremental via content-hash cache (e.g. "2/5 files served from cache"). Verified: edit (+node/+edges), add file, delete file (nodes and edges removed), rename (old removed, new added), mass delete (10 of 14 nodes removed) — all correct and exit 0. The documented `--force` fewer-nodes guard never triggered in these tests |
| Failure behaviour | Corrupt `graph.json` → `update` refuses with a clear message and exit 1 (no silent overwrite); recovery = delete `graphify-out` and run a full `extract`, which worked. Missing path → exit 1. **A file with a syntax error is indexed silently** (tree-sitter error recovery produced nodes) — Graphify does not report per-file parse failures |
| Determinism | Two clean extractions of the same tree produced identical node, edge and community sets |
| `built_at_commit` | Set from Git `HEAD` of the scanned directory at scan time, **not** from the content that was scanned: an uncommitted edit was indexed under the old commit SHA. Outside a Git repo it is `null`. It cannot be trusted as provenance |
| Relationship quality | On this project: 597 nodes, 1506 edges in ~7 s; 1367 `EXTRACTED`, 139 `INFERRED`. Call-edge lookups matched reality (`prepare_event` ← `IngestionPipeline.process` and tests). Imprecise case seen: `handler()` → `OrderService().avg()` yielded an edge to the class, not to the `.avg()` method |

## Decision (recommended)

**Adopt Graphify** as the code-relationship adapter, under these constraints:

1. **Isolated CLI, pinned.** Install `graphifyy==0.9.97` (wheel sha256 `eb24dd7f93293753c001ca36e7304e0b52e86fe4124c2f531dcdb3d64865e01a`) in its own virtual environment, not in TraceMind's. TraceMind calls it only through a subprocess with a fixed argv (no shell), a timeout, a scrubbed environment (no API keys, no proxies carrying credentials) and bounded output. Only `extract <dir> --code-only` and `update <dir>` are ever invoked.
2. **Scan snapshots, never the monitored repository.** Graphify writes into the scanned directory, so TraceMind scans a TraceMind-owned snapshot of an exact commit produced by `git archive` (symlinks and unsafe paths rejected). The monitored repository is only read — SOW §13 read-only boundary.
3. **Provenance is ours.** The scan record stores the commit SHA TraceMind exported, the Graphify version, `graph.json` `schema_version` and a SHA-256 of `graph.json`. `built_at_commit` and `.graphify_root` are ignored.
4. **Validate before use.** `graph.json` must parse, have `schema_version == 1`, and match expected shapes; otherwise the scan fails and the last good graph is kept. Unknown relations or fields are tolerated and ignored.
5. **Label uncertainty.** Every relationship returned to callers carries `confidence` (`EXTRACTED`/`INFERRED`), `source_file` and line. `INFERRED` is never presented as fact. Absence of an edge is not evidence of independence.
6. **Parse failures are ours to detect.** Because Graphify does not report them, TraceMind records scan coverage (files seen vs files in the graph) and does not claim a file was understood merely because it has nodes.
7. **Recovery.** Update failure → keep the previous graph and mark the scan `failed` with a reason; corrupt graph → delete the graph directory and run a full rebuild, with bounded retries.
8. **Labelled as derived.** Graphify output never overrides Git or source; the API names it as derived, with its version.

## Fallback (if the owner rejects adoption, or if a later check fails)

A limited Python-only index using the standard-library `ast` module (imports, definitions, direct calls), producing the same internal `CodeGraph` shape. It would be documented as *not* equivalent to Graphify (single language, no cross-language resolution, no confidence inference). The adapter boundary below makes this swap a contained change.

## Consequences

- Positive: no LLM or paid API in the code path; deterministic; incremental; multi-language headroom; Apache-2.0.
- Negative: a second Python environment to install and pin; a young tool with a fast release cadence (0.9.x) and an undocumented-as-stable JSON format, so schema/version checks and golden-file tests are mandatory; method-call resolution is partial; no parse-error reporting.
- TraceMind's own code depends on an internal `CodeGraph` interface, not on Graphify's file format, so the format can change in one module.

## Not verified (open items)

TypeScript/Go/Java/etc. extraction; behaviour and timing on large repositories; symlink handling inside a scanned tree (mitigated by exporting without symlinks); the `--force` guard condition; Linux; whether any code path outside the documented LLM/`add` commands makes network calls (only tested with a dead proxy); the project's governance and security posture beyond its license metadata.

## Addendum (2026-10-28, from M2 implementation)

Implemented as decided. Behaviours discovered during implementation that refine the constraints above are recorded in [dependency-verification.md](../dependency-verification.md): paths depend on the working directory, `.gitignore` handling differs between `extract` and `update`, `--no-cluster` drops the metadata block, `update` adds document nodes, and builds are byte-identical. The runner therefore always runs inside the snapshot with `extract . --code-only --no-gitignore` or `update .`; the loader maps and validates every path and keeps only code nodes.

Additional hardening beyond constraint 1: the whole dependency tree (not just Graphify) is installed from a hash-pinned lock, and before use the runner verifies the installed files against their RECORD hashes; a mismatch is `graphify_install_modified` and the worker refuses to start. The Python `ast` fallback remains documented and **not implemented**; no check so far required it.
