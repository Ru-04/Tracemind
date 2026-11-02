"""Every SQL statement TraceMind may send to Snowflake, as a static constant (check M3-2).

Rules, enforced mechanically by tests/unit/test_snowflake_sql.py:
- statements are built once, at import, from fixed column lists in this file; nothing at run
  time formats, concatenates or interpolates a value, an identifier or a caller string;
- values travel only as `?` bind parameters (server-side binding);
- reader statements are one `SELECT` over the reader VIEWS and nothing else;
- writer statements touch only the three tables, the session's own TEMPORARY batch tables, and
  contain no `UPDATE`, `DELETE`, `TRUNCATE`, `DROP` or `WHEN MATCHED`.

UNVERIFIED until the live checks M3-12 to M3-14 (no account has run any of this):
- the privileges `MERGE` needs (the design assumes INSERT + SELECT on the target; no official
  page states it), and what a blocked MERGE does after its lock is released;
- `QUALIFY` and `PARSE_JSON` inside the `USING` subquery, bulk-bound `executemany` into a
  temporary table, the shape of a MERGE's result row, and `LIMIT ?` as a bind parameter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Col:
    name: str  # column of the temporary batch table (and of the source row)
    tmp_type: str
    target: str | None = None  # column of the target table (default: same name)
    expr: str | None = None  # expression over the temporary column (default: the column)

    @property
    def target_name(self) -> str:
        return self.target or self.name

    @property
    def source_expr(self) -> str:
        return self.expr or self.name


@dataclass(frozen=True)
class StreamSql:
    stream: str
    target: str
    tmp: str
    key: tuple[str, ...]
    columns: tuple[Col, ...]
    create_tmp: str
    insert_tmp: str
    merge: str
    audit_batch: str
    audit_all: str

    @property
    def params_per_row(self) -> int:
        return len(self.columns)


def _stream(
    stream: str,
    target: str,
    tmp: str,
    key: tuple[str, ...],
    order_by: str,
    columns: tuple[Col, ...],
) -> StreamSql:
    names = [c.name for c in columns]
    targets = [c.target_name for c in columns]
    create_tmp = (
        f"CREATE OR REPLACE TEMPORARY TABLE {tmp} ("
        + ", ".join(f"{c.name} {c.tmp_type}" for c in columns)
        + ")"
    )
    insert_tmp = (
        f"INSERT INTO {tmp} ({', '.join(names)}) VALUES ({', '.join('?' for _ in columns)})"
    )
    merge = (
        f"MERGE INTO {target} AS t USING ("
        f"SELECT {', '.join(f'{c.source_expr} AS {c.target_name}' for c in columns)} FROM ("
        f"SELECT * FROM {tmp} "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {', '.join(key)} ORDER BY {order_by}) = 1)"
        ") AS s "
        f"ON {' AND '.join(f't.{k} = s.{k}' for k in key)} "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(targets)}) "
        f"VALUES ({', '.join(f's.{c}' for c in targets)})"
    )
    key_join = " AND ".join(f"t.{k} = b.{k}" for k in key)
    key_list = ", ".join(f"t.{k}" for k in key)
    audit_batch = (
        f"SELECT {key_list}, COUNT(*) AS n FROM {target} AS t JOIN {tmp} AS b ON {key_join} "
        f"GROUP BY {key_list} HAVING COUNT(*) > 1"
    )
    plain = ", ".join(key)
    audit_all = f"SELECT {plain}, COUNT(*) AS n FROM {target} GROUP BY {plain} HAVING COUNT(*) > 1"
    return StreamSql(
        stream, target, tmp, key, columns, create_tmp, insert_tmp, merge, audit_batch, audit_all
    )


EVENTS = _stream(
    "events",
    "TELEMETRY.EVENTS",
    "TMP_EVENTS_BATCH",
    ("project_id", "event_id"),
    "occurred_at",
    (
        Col("project_id", "STRING"),
        Col("event_id", "STRING"),
        Col("service", "STRING"),
        Col("environment", "STRING"),
        Col("occurred_at", "STRING", expr="TO_TIMESTAMP_TZ(occurred_at)"),
        Col("severity", "STRING"),
        Col("error_type", "STRING"),
        Col("message", "STRING"),
        Col("fingerprint", "STRING"),
        Col("fingerprint_version", "NUMBER(38,0)"),
        Col("trace_id", "STRING"),
        Col("commit_sha", "STRING"),
        Col("release", "STRING"),
        Col("attributes_json", "STRING", target="attributes", expr="PARSE_JSON(attributes_json)"),
        Col("test_run_id", "STRING"),
    ),
)

INCIDENT_SUMMARIES = _stream(
    "incident_summaries",
    "KNOWLEDGE.INCIDENT_SUMMARIES",
    "TMP_INCIDENT_SUMMARIES_BATCH",
    ("project_id", "incident_id", "summary_version"),
    "last_seen",
    (
        Col("project_id", "STRING"),
        Col("incident_id", "STRING"),
        Col("summary_version", "STRING"),
        Col("service", "STRING"),
        Col("environment", "STRING"),
        Col("fingerprint", "STRING"),
        Col("fingerprint_version", "NUMBER(38,0)"),
        Col("error_type", "STRING"),
        Col("severity", "STRING"),
        Col("status", "STRING"),
        Col("first_seen", "STRING", expr="TO_TIMESTAMP_TZ(first_seen)"),
        Col("last_seen", "STRING", expr="TO_TIMESTAMP_TZ(last_seen)"),
        Col("occurrence_count", "NUMBER(38,0)"),
        Col("reopen_count", "NUMBER(38,0)"),
        Col("summary", "STRING"),
        Col("test_run_id", "STRING"),
    ),
)

DEPLOYMENTS = _stream(
    "deployments",
    "KNOWLEDGE.DEPLOYMENTS",
    "TMP_DEPLOYMENTS_BATCH",
    ("project_id", "deployment_id"),
    "deployed_at",
    (
        Col("project_id", "STRING"),
        Col("deployment_id", "STRING"),
        Col("environment", "STRING"),
        Col("service", "STRING"),
        Col("release", "STRING"),
        Col("deployed_at", "STRING", expr="TO_TIMESTAMP_TZ(deployed_at)"),
        Col("commit_sha", "STRING"),
        Col("commit_confidence", "STRING"),
        Col("test_run_id", "STRING"),
    ),
)

STREAMS: dict[str, StreamSql] = {s.stream: s for s in (EVENTS, INCIDENT_SUMMARIES, DEPLOYMENTS)}

# ---- reader statements (named query functions, M3 plan 4.6) ----------------------------------

EVENTS_FOR_FINGERPRINT = (
    "SELECT event_id, service, environment, occurred_at, severity, error_type, message, "
    "fingerprint, release, commit_sha, trace_id FROM TELEMETRY.V_EVENTS "
    "WHERE project_id = ? AND fingerprint = ? "
    "AND occurred_at >= TO_TIMESTAMP_TZ(?) AND occurred_at < TO_TIMESTAMP_TZ(?) "
    "ORDER BY occurred_at DESC, event_id LIMIT ?"
)

EVENTS_FOR_SERVICE_WINDOW = (
    "SELECT event_id, service, environment, occurred_at, severity, error_type, message, "
    "fingerprint, release, commit_sha, trace_id FROM TELEMETRY.V_EVENTS "
    "WHERE project_id = ? AND service = ? AND environment = ? "
    "AND occurred_at >= TO_TIMESTAMP_TZ(?) AND occurred_at < TO_TIMESTAMP_TZ(?) "
    "ORDER BY occurred_at DESC, event_id LIMIT ?"
)

EVENTS_FOR_COMMIT = (
    "SELECT event_id, service, environment, occurred_at, severity, error_type, message, "
    "fingerprint, release, commit_sha, trace_id FROM TELEMETRY.V_EVENTS "
    "WHERE project_id = ? AND commit_sha = ? "
    "AND occurred_at >= TO_TIMESTAMP_TZ(?) AND occurred_at < TO_TIMESTAMP_TZ(?) "
    "ORDER BY occurred_at DESC, event_id LIMIT ?"
)

_INCIDENT_COLUMNS = (
    "incident_id, service, environment, fingerprint, error_type, severity, status, "
    "first_seen, last_seen, occurrence_count, reopen_count, summary"
)

SIMILAR_BY_FINGERPRINT = (
    f"SELECT {_INCIDENT_COLUMNS} FROM KNOWLEDGE.V_INCIDENT_SUMMARIES "
    "WHERE project_id = ? AND fingerprint = ? AND incident_id <> ? "
    "AND last_seen >= TO_TIMESTAMP_TZ(?) AND last_seen < TO_TIMESTAMP_TZ(?) "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY incident_id ORDER BY summary_version DESC) = 1 "
    "ORDER BY last_seen DESC, incident_id LIMIT ?"
)

SIMILAR_BY_SERVICE_ERROR = (
    f"SELECT {_INCIDENT_COLUMNS} FROM KNOWLEDGE.V_INCIDENT_SUMMARIES "
    "WHERE project_id = ? AND service = ? AND error_type = ? AND incident_id <> ? "
    "AND last_seen >= TO_TIMESTAMP_TZ(?) AND last_seen < TO_TIMESTAMP_TZ(?) "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY incident_id ORDER BY summary_version DESC) = 1 "
    "ORDER BY last_seen DESC, incident_id LIMIT ?"
)

DOCUMENTED_RESOLUTIONS = (
    "SELECT incident_id, fingerprint, resolution_state, note, recorded_at "
    "FROM KNOWLEDGE.V_RESOLUTIONS WHERE project_id = ? AND fingerprint = ? "
    "ORDER BY recorded_at DESC, incident_id LIMIT ?"
)

#: function name -> statement. The number of parameters is the number of `?` in the statement;
#: tests compare every call's parameter tuple against it.
READER_TEMPLATES: dict[str, str] = {
    "events_for_fingerprint": EVENTS_FOR_FINGERPRINT,
    "events_for_service_window": EVENTS_FOR_SERVICE_WINDOW,
    "events_for_verified_commit": EVENTS_FOR_COMMIT,
    "similar_incidents_by_fingerprint": SIMILAR_BY_FINGERPRINT,
    "similar_incidents_by_service_error": SIMILAR_BY_SERVICE_ERROR,
    "documented_resolutions": DOCUMENTED_RESOLUTIONS,
}

READER_VIEWS = (
    "TELEMETRY.V_EVENTS",
    "KNOWLEDGE.V_INCIDENT_SUMMARIES",
    "KNOWLEDGE.V_DEPLOYMENTS",
    "KNOWLEDGE.V_RESOLUTIONS",
)
