"""Snowflake settings, read from the environment (names: `.env.example`, setup guide 13.1).

Rules enforced here, all offline:
- everything is OFF by default (`TRACEMIND_SNOWFLAKE_ENABLED=false`);
- the export interval has a code-enforced floor (owner decision O4): start-up is refused below it;
- a private key is referenced by PATH only, the path must lie outside the repository, and the key
  passphrase lives in a `Secret` that never renders its value (repr, str, logs, errors);
- messages name settings, never their values.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tracemind.config import ConfigError

EXPORT_INTERVAL_FLOOR_SECONDS = 7200  # owner decision O4: never export more often than this
EXPORT_INTERVAL_DEFAULT_SECONDS = 14400
SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}\Z")
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_AUTHENTICATORS = ("SNOWFLAKE_JWT",)
TEST_RUN_ID = re.compile(r"^tmtest-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")


class Secret:
    """Holds a secret and refuses to show it. `reveal()` is the only way out and is called in
    exactly one place: the connector adapter."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(("Secret", len(self._value)))

    def __reduce__(self) -> tuple[()]:  # pragma: no cover - refuses pickling/copying
        raise TypeError("Secret cannot be pickled")

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: object) -> Secret:
        return self


@dataclass(frozen=True)
class ServiceCredentials:
    """One Snowflake service user. Holds a key *path* and the passphrase, never key bytes."""

    user: str
    role: str
    private_key_file: Path | None = None
    passphrase: Secret | None = field(default=None, repr=False)


def _ident(value: str, setting: str) -> str:
    if not _IDENT.match(value):
        raise ConfigError(f"{setting} is not a valid Snowflake identifier")
    return value


@dataclass(frozen=True)
class SnowflakeSettings:
    enabled: bool = False
    account: str = ""
    authenticator: str = "SNOWFLAKE_JWT"
    warehouse: str = "TRACEMIND_WH"
    database: str = "TRACEMIND_DEV"
    test_database: str = "TRACEMIND_TEST"
    writer: ServiceCredentials = ServiceCredentials("TRACEMIND_EXPORTER_SVC", "TRACEMIND_WRITER")
    reader: ServiceCredentials = ServiceCredentials("TRACEMIND_QUERY_SVC", "TRACEMIND_READER")
    export_min_interval_seconds: int = EXPORT_INTERVAL_DEFAULT_SECONDS
    export_batch_max_rows: int = 1000
    export_min_severity: str = "ERROR"
    query_timeout_seconds: int = 30
    lock_timeout_seconds: int = 60
    max_rows: int = 500
    max_concurrency: int = 2
    soft_budget_credits: float = 15.0

    def __post_init__(self) -> None:
        if self.export_min_interval_seconds < EXPORT_INTERVAL_FLOOR_SECONDS:
            raise ConfigError(
                "TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS is below the enforced floor of "
                f"{EXPORT_INTERVAL_FLOOR_SECONDS} seconds"
            )
        if self.export_min_severity not in SEVERITIES:
            raise ConfigError("TRACEMIND_SNOWFLAKE_EXPORT_MIN_SEVERITY is not a known severity")
        bounds = {
            "TRACEMIND_SNOWFLAKE_EXPORT_BATCH_MAX_ROWS": (self.export_batch_max_rows, 1, 10_000),
            "TRACEMIND_SNOWFLAKE_QUERY_TIMEOUT_SECONDS": (self.query_timeout_seconds, 1, 300),
            "TRACEMIND_SNOWFLAKE_LOCK_TIMEOUT_SECONDS": (self.lock_timeout_seconds, 1, 600),
            "TRACEMIND_SNOWFLAKE_MAX_ROWS": (self.max_rows, 1, 10_000),
            "TRACEMIND_SNOWFLAKE_MAX_CONCURRENCY": (self.max_concurrency, 1, 16),
        }
        for name, (value, low, high) in bounds.items():
            if not low <= value <= high:
                raise ConfigError(f"{name} must be between {low} and {high}")
        if not 0 < self.soft_budget_credits <= 10_000:
            raise ConfigError("TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS must be positive")
        for setting, identifier in (
            ("SNOWFLAKE_WAREHOUSE", self.warehouse),
            ("SNOWFLAKE_DATABASE", self.database),
            ("SNOWFLAKE_TEST_DATABASE", self.test_database),
        ):
            _ident(identifier, setting)
        # database == test_database is allowed on purpose: that is how a live test run points the
        # exporter at the test database, and the exporter then REQUIRES a test run id.
        for who, creds in (("WRITER", self.writer), ("READER", self.reader)):
            _ident(creds.user, f"SNOWFLAKE_{who}_USER")
            _ident(creds.role, f"SNOWFLAKE_{who}_ROLE")
        if self.writer.user == self.reader.user or self.writer.role == self.reader.role:
            raise ConfigError(
                "the writer and the reader must be different users with different roles"
            )
        if self.enabled:
            self._validate_enabled()

    def _validate_enabled(self) -> None:
        if not _ACCOUNT.match(self.account):
            raise ConfigError("SNOWFLAKE_ACCOUNT is required when Snowflake is enabled")
        if self.authenticator not in _AUTHENTICATORS:
            raise ConfigError("SNOWFLAKE_AUTHENTICATOR must be SNOWFLAKE_JWT (key-pair)")
        for who, creds in (("WRITER", self.writer), ("READER", self.reader)):
            if creds.private_key_file is None:
                raise ConfigError(f"SNOWFLAKE_{who}_PRIVATE_KEY_FILE is required when enabled")
            if not creds.passphrase:
                raise ConfigError(
                    f"SNOWFLAKE_{who}_PRIVATE_KEY_PASSPHRASE is required when enabled"
                )
        if self.writer.private_key_file == self.reader.private_key_file:
            raise ConfigError("the writer and the reader must use different key files")

    def key_files_outside(self, forbidden_roots: Sequence[Path]) -> None:
        """Refuse a key stored inside any forbidden tree (the repository). Called at start-up,
        when `enabled`, not at import, so tests need no key."""
        for who, creds in (("WRITER", self.writer), ("READER", self.reader)):
            if creds.private_key_file is None:
                continue
            resolved = creds.private_key_file.expanduser().resolve()
            for root in forbidden_roots:
                base = root.resolve()
                if resolved == base or base in resolved.parents:
                    raise ConfigError(
                        f"SNOWFLAKE_{who}_PRIVATE_KEY_FILE must be outside the repository"
                    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SnowflakeSettings:
        e = os.environ if env is None else env
        d = cls()

        def text(name: str, default: str) -> str:
            return (e.get(name) or default).strip()

        def number(name: str, default: int) -> int:
            raw = e.get(name)
            return default if raw in (None, "") else int(str(raw))

        def creds(prefix: str, default: ServiceCredentials) -> ServiceCredentials:
            key = e.get(f"SNOWFLAKE_{prefix}_PRIVATE_KEY_FILE", "").strip()
            phrase = e.get(f"SNOWFLAKE_{prefix}_PRIVATE_KEY_PASSPHRASE", "")
            return ServiceCredentials(
                user=text(f"SNOWFLAKE_{prefix}_USER", default.user),
                role=text(f"SNOWFLAKE_{prefix}_ROLE", default.role),
                private_key_file=Path(key) if key else None,
                passphrase=Secret(phrase) if phrase else None,
            )

        try:
            return cls(
                enabled=e.get("TRACEMIND_SNOWFLAKE_ENABLED", "false").strip().lower()
                in ("1", "true", "yes"),
                account=text("SNOWFLAKE_ACCOUNT", ""),
                authenticator=text("SNOWFLAKE_AUTHENTICATOR", d.authenticator),
                warehouse=text("SNOWFLAKE_WAREHOUSE", d.warehouse),
                database=text("SNOWFLAKE_DATABASE", d.database),
                test_database=text("SNOWFLAKE_TEST_DATABASE", d.test_database),
                writer=creds("WRITER", d.writer),
                reader=creds("READER", d.reader),
                export_min_interval_seconds=number(
                    "TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS", d.export_min_interval_seconds
                ),
                export_batch_max_rows=number(
                    "TRACEMIND_SNOWFLAKE_EXPORT_BATCH_MAX_ROWS", d.export_batch_max_rows
                ),
                export_min_severity=text(
                    "TRACEMIND_SNOWFLAKE_EXPORT_MIN_SEVERITY", d.export_min_severity
                ).upper(),
                query_timeout_seconds=number(
                    "TRACEMIND_SNOWFLAKE_QUERY_TIMEOUT_SECONDS", d.query_timeout_seconds
                ),
                lock_timeout_seconds=number(
                    "TRACEMIND_SNOWFLAKE_LOCK_TIMEOUT_SECONDS", d.lock_timeout_seconds
                ),
                max_rows=number("TRACEMIND_SNOWFLAKE_MAX_ROWS", d.max_rows),
                max_concurrency=number("TRACEMIND_SNOWFLAKE_MAX_CONCURRENCY", d.max_concurrency),
                soft_budget_credits=float(
                    e.get("TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS") or d.soft_budget_credits
                ),
            )
        except ValueError as exc:
            raise ConfigError(
                "a numeric TRACEMIND_SNOWFLAKE_* setting is not a valid number"
            ) from exc
