from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tracemind.config import Settings
from tracemind.ingestion.pipeline import IngestionPipeline
from tracemind.projects import Project
from tracemind.storage.postgres import PostgresStore

from tests.conftest import NOW, ROOT

TABLES = (
    "snowflake_calls, export_state, export_marks, "
    "deployments, incident_transitions, scan_events, change_records, repository_commits, "
    "repository_scans, repositories, ingestion_rejections, ingestion_stats, "
    "incident_occurrences, incidents, events"
)


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "backend/tracemind/storage/migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[sa.Engine]:
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "base")  # also proves the downgrade path
    command.upgrade(cfg, "head")
    engine = sa.create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def engine(migrated_engine: sa.Engine) -> Iterator[sa.Engine]:
    with migrated_engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {TABLES}"))
    yield migrated_engine


@pytest.fixture
def store(engine: sa.Engine) -> PostgresStore:
    return PostgresStore(engine)


@pytest.fixture
def pipeline(store: PostgresStore, settings: Settings) -> IngestionPipeline:
    return IngestionPipeline(store, settings, clock=lambda: NOW)


@pytest.fixture
def project() -> Project:
    return Project("project-demo", frozenset({"payments-api", "demo-payments"}))


@pytest.fixture
def migrations_dir() -> Path:
    return ROOT / "backend/tracemind/storage/migrations"


@pytest.fixture
def env(engine, tmp_path, graphify_bin):  # type: ignore[no-untyped-def]
    from tests.integration.scan_helpers import build_env

    return build_env(engine, tmp_path, graphify_bin)
