from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import context

config = context.config


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("TRACEMIND_DATABASE_URL")
    if not url:
        raise RuntimeError("Set TRACEMIND_DATABASE_URL or pass sqlalchemy.url")
    return url


def run_migrations() -> None:
    engine = sa.create_engine(_url(), poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
