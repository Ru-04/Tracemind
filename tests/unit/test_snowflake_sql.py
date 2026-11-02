"""M3-1 (static half) and M3-2: the SQL TraceMind can send to Snowflake is a closed set of static
statements, and they agree with the versioned schema script. Nothing here needs an account.

What these tests CANNOT show: that Snowflake accepts the statements or that the privileges are
sufficient. That is the live half of M3-1/M3-2 and M3-12/M3-13, which have not been run."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tracemind.snowflake import sql as S

from tests.conftest import ROOT

SF = ROOT / "backend/tracemind/snowflake"
DDL = (ROOT / "infra/snowflake/schema/V001__telemetry_and_knowledge.sql").read_text(
    encoding="utf-8"
)
WRITER_TABLES = {s.target for s in S.STREAMS.values()}
TMP_TABLES = {s.tmp for s in S.STREAMS.values()}


def writer_statements() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for spec in S.STREAMS.values():
        for kind in ("create_tmp", "insert_tmp", "merge", "audit_batch", "audit_all"):
            out.append((f"{spec.stream}.{kind}", getattr(spec, kind)))
    return out


def reader_statements() -> list[tuple[str, str]]:
    return list(S.READER_TEMPLATES.items())


ALL = writer_statements() + reader_statements()


# ---- shape of every statement --------------------------------------------------------------


@pytest.mark.parametrize(("name", "statement"), ALL)
def test_statements_are_static_single_and_use_only_bind_parameters(
    name: str, statement: str
) -> None:
    assert "{" not in statement and "}" not in statement, name  # no leftover format fields
    assert ";" not in statement, name  # one statement per call
    assert "'" not in statement and '"' not in statement, name  # no literals, only binds
    assert "--" not in statement and "/*" not in statement, name
    assert "\n" not in statement, name


@pytest.mark.parametrize(("name", "statement"), writer_statements())
def test_writer_statements_cannot_change_or_remove_rows(name: str, statement: str) -> None:
    upper = statement.upper()
    for word in (
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "DROP",
        "GRANT",
        "REVOKE",
        "ALTER",
        "WHEN MATCHED",
    ):
        assert re.search(rf"\b{word}\b", upper) is None, f"{name} contains {word}"
    # CREATE appears exactly once and only for the session's own TEMPORARY batch table
    if name.endswith("create_tmp"):
        assert statement.startswith("CREATE OR REPLACE TEMPORARY TABLE TMP_")
    else:
        assert "CREATE" not in upper


@pytest.mark.parametrize("spec", list(S.STREAMS.values()), ids=lambda s: s.stream)
def test_merge_is_insert_only_on_the_logical_key(spec: S.StreamSql) -> None:
    m = spec.merge
    assert m.count("WHEN NOT MATCHED THEN INSERT") == 1
    assert "WHEN MATCHED" not in m
    assert m.startswith(f"MERGE INTO {spec.target} AS t USING (")
    # the match condition covers every key column, nothing else
    on = re.search(r" ON (.*?) WHEN NOT MATCHED", m)
    assert on is not None
    assert on.group(1).split(" AND ") == [f"t.{k} = s.{k}" for k in spec.key]
    # duplicates inside the batch are removed in the USING subquery as well (M3 plan 4.5, 3)
    assert f"PARTITION BY {', '.join(spec.key)}" in m and "ROW_NUMBER()" in m
    # the id of the test run is carried so test rows can be recognised and cleaned up
    assert any(c.name == "test_run_id" for c in spec.columns)


@pytest.mark.parametrize("spec", list(S.STREAMS.values()), ids=lambda s: s.stream)
def test_writer_statements_touch_only_the_three_tables_and_their_own_batch_tables(
    spec: S.StreamSql,
) -> None:
    allowed = WRITER_TABLES | TMP_TABLES
    for kind in ("create_tmp", "insert_tmp", "merge", "audit_batch", "audit_all"):
        statement = getattr(spec, kind)
        names = set(re.findall(r"\b(?:INTO|FROM|JOIN|TABLE)\s+([A-Z_]+(?:\.[A-Z_]+)?)", statement))
        assert names <= allowed, (spec.stream, kind, names - allowed)
        assert "META." not in statement and "V_" not in statement


@pytest.mark.parametrize(("name", "statement"), reader_statements())
def test_reader_statements_are_one_select_over_reader_views(name: str, statement: str) -> None:
    assert statement.startswith("SELECT ")
    for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "GRANT", "CALL"):
        assert re.search(rf"\b{word}\b", statement) is None, f"{name} contains {word}"
    sources = re.findall(r"\b(?:FROM|JOIN)\s+([A-Z_]+\.[A-Z_]+)", statement)
    assert sources and set(sources) <= set(S.READER_VIEWS), (name, sources)
    # project scoping is the first predicate of every function, bound, never inlined
    assert " WHERE project_id = ? " in statement, name
    assert statement.split(" WHERE ", 1)[1].startswith("project_id = ?"), name
    # a hard row cap is bound as the last parameter
    assert statement.endswith(" LIMIT ?"), name


def test_reader_functions_are_exactly_the_planned_set() -> None:
    assert set(S.READER_TEMPLATES) == {
        "events_for_fingerprint",
        "events_for_service_window",
        "events_for_verified_commit",
        "similar_incidents_by_fingerprint",
        "similar_incidents_by_service_error",
        "documented_resolutions",
    }


def test_bind_placeholders_match_the_batch_columns() -> None:
    for spec in S.STREAMS.values():
        assert spec.insert_tmp.count("?") == len(spec.columns)
        names = re.search(r"\((.*?)\) VALUES", spec.insert_tmp)
        assert names is not None
        assert names.group(1).split(", ") == [c.name for c in spec.columns]


# ---- no way to send other SQL --------------------------------------------------------------------

_SQL_START = re.compile(
    r"^\s*(SELECT|MERGE|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|USE|TRUNCATE|GRANT)\b"
)
# PostgreSQL (not Snowflake) statements used for the single-exporter advisory lock.
_POSTGRES_ALLOWED = {"SELECT pg_try_advisory_lock(:k)", "SELECT pg_advisory_unlock(:k)"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_no_sql_text_is_written_anywhere_but_sql_py() -> None:
    offenders: list[str] = []
    for path in sorted(SF.glob("*.py")):
        if path.name == "sql.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docs
                and _SQL_START.match(node.value)
                and node.value not in _POSTGRES_ALLOWED
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_session_calls_never_receive_a_literal_or_built_string() -> None:
    """Every `session.execute(...)` / `session.executemany(...)` passes a name or attribute (a
    template), never a string literal, f-string, concatenation or call result."""
    checked = 0
    for path in sorted(SF.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executemany")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("session", "_inner")
            ):
                checked += 1
                assert isinstance(node.args[0], ast.Name | ast.Attribute), (
                    f"{path.name}:{node.lineno} passes something other than a template"
                )
    assert checked >= 5  # the check itself must be looking at the real call sites


# ---- agreement with the versioned schema script (M3-1, static half) ------------------------------


def _tables() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\S+) \((.*?)\n\);", DDL, re.S):
        cols: dict[str, str] = {}
        for line in m.group(2).splitlines():
            line = line.split("--")[0].strip().rstrip(",")
            if line:
                name, _, rest = line.partition(" ")
                cols[name.lower()] = rest.strip()
        out[m.group(1)] = cols
    return out


def _views() -> dict[str, tuple[list[str], str]]:
    out: dict[str, tuple[list[str], str]] = {}
    for m in re.finditer(
        r"CREATE VIEW IF NOT EXISTS (\S+) AS\s+SELECT (.*?)\s+FROM (\S+);", DDL, re.S
    ):
        out[m.group(1)] = ([c.strip() for c in m.group(2).split(",")], m.group(3))
    return out


def test_script_defines_the_expected_objects() -> None:
    assert set(_tables()) == {
        "META.SCHEMA_VERSION",
        "TELEMETRY.EVENTS",
        "KNOWLEDGE.INCIDENT_SUMMARIES",
        "KNOWLEDGE.DEPLOYMENTS",
        "KNOWLEDGE.INVESTIGATION_RESULTS",
    }
    assert set(_views()) == set(S.READER_VIEWS)


def test_script_is_idempotent_and_never_destructive() -> None:
    body = "\n".join(line.split("--")[0] for line in DDL.splitlines())
    for word in ("DROP", "TRUNCATE", "DELETE", "UPDATE", "GRANT", "REVOKE", "ALTER", "REPLACE"):
        assert re.search(rf"\b{word}\b", body, re.I) is None, word
    assert len(re.findall(r"\bCREATE\b", body)) == len(re.findall(r"IF NOT EXISTS", body))
    assert "INSERT INTO META.SCHEMA_VERSION" in body and "WHERE NOT EXISTS" in body


def test_script_declares_no_constraints_because_snowflake_does_not_enforce_them() -> None:
    body = "\n".join(line.split("--")[0] for line in DDL.splitlines()).upper()
    for word in ("PRIMARY KEY", "UNIQUE", "CONSTRAINT", "FOREIGN KEY", "REFERENCES"):
        assert word not in body


def test_script_is_guarded_against_run_all_and_runs_as_the_migrator_with_secondary_roles_off() -> (
    None
):
    guards = re.findall(r"^SELECT <<[A-Z0-9_]+__DELETE_THIS_LINE>>;", DDL, re.M)
    assert len(guards) == 1
    assert DDL.index(guards[0]) < DDL.index("USE ROLE TRACEMIND_MIGRATOR;")
    assert DDL.index("USE ROLE TRACEMIND_MIGRATOR;") < DDL.index("USE SECONDARY ROLES NONE;")
    assert DDL.index("USE SECONDARY ROLES NONE;") < DDL.index("CREATE TABLE")


def test_every_data_table_carries_a_nullable_test_run_id_and_views_hide_it() -> None:
    for name, cols in _tables().items():
        if name.startswith("META."):
            continue
        assert cols.get("test_run_id") == "STRING", name  # nullable: no NOT NULL
    for name, (cols, _source) in _views().items():
        assert "test_run_id" not in cols, name


@pytest.mark.parametrize("spec", list(S.STREAMS.values()), ids=lambda s: s.stream)
def test_loader_columns_match_the_table(spec: S.StreamSql) -> None:
    table = _tables()[spec.target]
    targets = [c.target_name for c in spec.columns]
    assert len(set(targets)) == len(targets)
    assert set(targets) <= set(table), set(targets) - set(table)
    # every column the loader does NOT set must be nullable or have a default
    for col, definition in table.items():
        if col not in targets:
            assert "NOT NULL" not in definition or "DEFAULT" in definition, (spec.target, col)
    # the logical key columns exist and are NOT NULL
    for key in spec.key:
        assert "NOT NULL" in table[key], (spec.target, key)


def test_reader_statements_select_only_columns_the_views_expose() -> None:
    views = _views()
    for name, statement in S.READER_TEMPLATES.items():
        view = re.search(r"\bFROM (\S+)", statement)
        assert view is not None
        exposed = {c.lower() for c in views[view.group(1)][0]}
        # Keywords and functions are upper case in the templates and identifiers lower case, so
        # every lower-case word is a column: in SELECT, WHERE, QUALIFY and ORDER BY alike.
        used = set(re.findall(r"\b[a-z_]+\b", statement))
        assert used and used <= exposed, (name, used - exposed)


def test_views_read_only_from_their_own_tables() -> None:
    tables = _tables()
    for name, (cols, source) in _views().items():
        assert source in tables, name
        assert {c.lower() for c in cols} <= set(tables[source]), name


def test_script_contains_no_secret_shaped_text() -> None:
    assert (
        re.search(r"BEGIN (ENCRYPTED |RSA )?PRIVATE KEY|PASSWORD\s*=|PRIVATE_KEY", DDL, re.I)
        is None
    )
    assert re.search(r"[A-Za-z0-9+/]{40,}", DDL) is None


def test_grants_live_only_in_the_admin_script() -> None:
    """The privilege model is unchanged by this work: no UPDATE/TRUNCATE is granted anywhere
    and DELETE is granted only to the test cleanup role on TRACEMIND_TEST."""
    admin = (ROOT / "infra/snowflake/01_admin_setup.sql").read_text(encoding="utf-8")
    grants = [
        line.split("--")[0].strip()
        for line in admin.splitlines()
        if line.strip().upper().startswith("GRANT ")
    ]
    for grant in grants:
        upper = grant.upper()
        assert "UPDATE" not in upper and "TRUNCATE" not in upper, grant
        if "DELETE" in upper:
            assert "TRACEMIND_TEST_CLEANER" in upper and "TRACEMIND_TEST." in upper, grant
