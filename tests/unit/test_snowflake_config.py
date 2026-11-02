"""M3-7 (no credential in source, logs, errors, repr) and the configuration rules the owner decided:
off by default, a code-enforced 7,200 s export floor, key files outside the repository."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
from tracemind.config import ConfigError
from tracemind.snowflake.config import (
    EXPORT_INTERVAL_DEFAULT_SECONDS,
    EXPORT_INTERVAL_FLOOR_SECONDS,
    TEST_RUN_ID,
    Secret,
    ServiceCredentials,
    SnowflakeSettings,
)

from tests.conftest import ROOT

PHRASE_W = "writer-passphrase-VALUE-123"
PHRASE_R = "reader-passphrase-VALUE-456"


def enabled_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "TRACEMIND_SNOWFLAKE_ENABLED": "true",
        "SNOWFLAKE_ACCOUNT": "myorg-myaccount",
        "SNOWFLAKE_WRITER_PRIVATE_KEY_FILE": str(tmp_path / "keys" / "writer.p8"),
        "SNOWFLAKE_WRITER_PRIVATE_KEY_PASSPHRASE": PHRASE_W,
        "SNOWFLAKE_READER_PRIVATE_KEY_FILE": str(tmp_path / "keys" / "reader.p8"),
        "SNOWFLAKE_READER_PRIVATE_KEY_PASSPHRASE": PHRASE_R,
        **extra,
    }


# ---- defaults and the floor -------------------------------------------------------------------


def test_everything_is_off_and_unconfigured_by_default() -> None:
    s = SnowflakeSettings.from_env({})
    assert s.enabled is False
    assert s.account == "" and s.writer.private_key_file is None and s.writer.passphrase is None
    assert s.export_min_interval_seconds == EXPORT_INTERVAL_DEFAULT_SECONDS == 14400
    assert (s.warehouse, s.database, s.test_database) == (
        "TRACEMIND_WH",
        "TRACEMIND_DEV",
        "TRACEMIND_TEST",
    )
    assert (s.writer.role, s.reader.role) == ("TRACEMIND_WRITER", "TRACEMIND_READER")
    assert s.soft_budget_credits == 15.0 and s.max_concurrency == 2 and s.max_rows == 500


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_only_explicit_true_values_enable(value: str, tmp_path: Path) -> None:
    assert SnowflakeSettings.from_env(
        enabled_env(tmp_path, TRACEMIND_SNOWFLAKE_ENABLED=value)
    ).enabled


@pytest.mark.parametrize("value", ["", "false", "0", "no", "enabled", "on", "2"])
def test_anything_else_stays_disabled(value: str) -> None:
    assert not SnowflakeSettings.from_env({"TRACEMIND_SNOWFLAKE_ENABLED": value}).enabled


def test_the_export_floor_is_7200_seconds_and_is_enforced_at_start_up() -> None:
    assert EXPORT_INTERVAL_FLOOR_SECONDS == 7200
    ok = SnowflakeSettings.from_env({"TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS": "7200"})
    assert ok.export_min_interval_seconds == 7200
    for bad in ("7199", "3600", "0", "-1"):
        with pytest.raises(ConfigError, match="floor of 7200"):
            SnowflakeSettings.from_env({"TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS": bad})
    # the floor applies even when Snowflake is disabled: a misconfiguration is never ignored
    with pytest.raises(ConfigError):
        SnowflakeSettings(enabled=False, export_min_interval_seconds=60)


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("TRACEMIND_SNOWFLAKE_EXPORT_BATCH_MAX_ROWS", "0"),
        ("TRACEMIND_SNOWFLAKE_EXPORT_BATCH_MAX_ROWS", "10001"),
        ("TRACEMIND_SNOWFLAKE_QUERY_TIMEOUT_SECONDS", "0"),
        ("TRACEMIND_SNOWFLAKE_QUERY_TIMEOUT_SECONDS", "301"),
        ("TRACEMIND_SNOWFLAKE_LOCK_TIMEOUT_SECONDS", "0"),
        ("TRACEMIND_SNOWFLAKE_MAX_ROWS", "0"),
        ("TRACEMIND_SNOWFLAKE_MAX_CONCURRENCY", "0"),
        ("TRACEMIND_SNOWFLAKE_MAX_CONCURRENCY", "17"),
        ("TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS", "0"),
        ("TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS", "-3"),
        ("TRACEMIND_SNOWFLAKE_EXPORT_MIN_SEVERITY", "LOUD"),
        ("SNOWFLAKE_WAREHOUSE", "bad name; DROP"),
        ("SNOWFLAKE_DATABASE", "x" * 300),
        ("SNOWFLAKE_WRITER_ROLE", "role with space"),
    ],
)
def test_out_of_range_or_malformed_values_are_refused(name: str, bad: str) -> None:
    with pytest.raises(ConfigError):
        SnowflakeSettings.from_env({name: bad})


def test_non_numeric_values_are_a_configuration_error_not_a_crash() -> None:
    for name in (
        "TRACEMIND_SNOWFLAKE_MAX_ROWS",
        "TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS",
        "TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS",
    ):
        with pytest.raises(ConfigError, match="not a valid number"):
            SnowflakeSettings.from_env({name: "twelve"})


def test_writer_and_reader_must_be_separate() -> None:
    with pytest.raises(ConfigError, match="different"):
        SnowflakeSettings.from_env({"SNOWFLAKE_READER_USER": "TRACEMIND_EXPORTER_SVC"})
    with pytest.raises(ConfigError, match="different"):
        SnowflakeSettings.from_env({"SNOWFLAKE_READER_ROLE": "TRACEMIND_WRITER"})


# ---- when enabled -----------------------------------------------------------------------------------


def test_enabled_needs_account_keys_and_passphrases(tmp_path: Path) -> None:
    good = enabled_env(tmp_path)
    assert SnowflakeSettings.from_env(good).enabled
    for drop, message in (
        ("SNOWFLAKE_ACCOUNT", "ACCOUNT"),
        ("SNOWFLAKE_WRITER_PRIVATE_KEY_FILE", "WRITER_PRIVATE_KEY_FILE"),
        ("SNOWFLAKE_WRITER_PRIVATE_KEY_PASSPHRASE", "WRITER_PRIVATE_KEY_PASSPHRASE"),
        ("SNOWFLAKE_READER_PRIVATE_KEY_FILE", "READER_PRIVATE_KEY_FILE"),
        ("SNOWFLAKE_READER_PRIVATE_KEY_PASSPHRASE", "READER_PRIVATE_KEY_PASSPHRASE"),
    ):
        env = {k: v for k, v in good.items() if k != drop}
        with pytest.raises(ConfigError, match=message):
            SnowflakeSettings.from_env(env)


def test_only_key_pair_authentication_is_accepted(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="SNOWFLAKE_JWT"):
        SnowflakeSettings.from_env(enabled_env(tmp_path, SNOWFLAKE_AUTHENTICATOR="SNOWFLAKE"))
    with pytest.raises(ConfigError, match="SNOWFLAKE_JWT"):
        SnowflakeSettings.from_env(
            enabled_env(tmp_path, SNOWFLAKE_AUTHENTICATOR="USERNAME_PASSWORD_MFA")
        )


def test_writer_and_reader_cannot_share_a_key_file(tmp_path: Path) -> None:
    shared = str(tmp_path / "one.p8")
    env = enabled_env(
        tmp_path,
        SNOWFLAKE_WRITER_PRIVATE_KEY_FILE=shared,
        SNOWFLAKE_READER_PRIVATE_KEY_FILE=shared,
    )
    with pytest.raises(ConfigError, match="different key files"):
        SnowflakeSettings.from_env(env)


def test_key_files_inside_the_repository_are_refused(tmp_path: Path) -> None:
    inside = enabled_env(
        tmp_path, SNOWFLAKE_WRITER_PRIVATE_KEY_FILE=str(ROOT / "config" / "writer.p8")
    )
    s = SnowflakeSettings.from_env(inside)
    with pytest.raises(ConfigError, match="outside the repository"):
        s.key_files_outside([ROOT])
    # a relative path that resolves into the repository is refused too
    rel = enabled_env(tmp_path, SNOWFLAKE_WRITER_PRIVATE_KEY_FILE="writer.p8")
    with pytest.raises(ConfigError):
        SnowflakeSettings.from_env(rel).key_files_outside([Path.cwd()])
    SnowflakeSettings.from_env(enabled_env(tmp_path)).key_files_outside([ROOT])  # outside: fine


# ---- secrets are never shown ---------------------------------------------------------------------------


def test_secret_never_renders_its_value() -> None:
    s = Secret("hunter2-super-secret")
    for text in (repr(s), str(s), f"{s}", f"{s!r}", "%s" % s, format(s)):  # noqa: UP031
        assert "hunter2" not in text
    assert s.reveal() == "hunter2-super-secret"
    assert bool(Secret("")) is False and bool(s) is True
    assert copy.copy(s) is s and copy.deepcopy(s) is s
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(s)


def test_settings_never_render_a_passphrase(tmp_path: Path) -> None:
    s = SnowflakeSettings.from_env(enabled_env(tmp_path))
    for text in (repr(s), str(s), repr(s.writer), repr(s.reader), str(s.writer)):
        assert PHRASE_W not in text and PHRASE_R not in text
    assert s.writer.passphrase is not None and s.writer.passphrase.reveal() == PHRASE_W


def test_error_messages_name_settings_never_values(tmp_path: Path) -> None:
    messages: list[str] = []
    for env in (
        enabled_env(tmp_path, SNOWFLAKE_AUTHENTICATOR="x" + PHRASE_W),
        enabled_env(tmp_path, TRACEMIND_SNOWFLAKE_MAX_ROWS=PHRASE_W),
        enabled_env(tmp_path, SNOWFLAKE_WAREHOUSE=PHRASE_R + " bad"),
        enabled_env(tmp_path, SNOWFLAKE_ACCOUNT="bad account " + PHRASE_R),
    ):
        with pytest.raises(ConfigError) as caught:
            SnowflakeSettings.from_env(env)
        messages.append(str(caught.value))
    for message in messages:
        assert PHRASE_W not in message and PHRASE_R not in message


def test_service_credentials_default_to_the_documented_names() -> None:
    assert ServiceCredentials("U", "R").passphrase is None


def test_test_run_id_format() -> None:
    assert TEST_RUN_ID.match("tmtest-20261102T101500Z-0a1b2c3d")
    for bad in (
        "",
        "tmtest-20261102T101500Z-0A1B2C3D",
        "tmtest-2026-11-02-0a1b2c3d",
        "tmtest-20261102T101500Z-0a1b2c3",
        "xtmtest-20261102T101500Z-0a1b2c3d",
        "tmtest-20261102T101500Z-0a1b2c3d' OR 1=1",
        "tmtest-20261102T101500Z-0a1b2c3d\n",
    ):
        assert TEST_RUN_ID.match(bad) is None, bad


# ---- .env.example agrees with the code -----------------------------------------------------------


def _example_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#?\s*((?:TRACEMIND_)?SNOWFLAKE_[A-Z_]+)=(.*)$", line)
        if m:
            names[m.group(1)] = m.group(2).split("#")[0].strip()
    return names


def test_every_variable_in_env_example_is_read_by_the_code() -> None:
    source = (ROOT / "backend/tracemind/snowflake/config.py").read_text(encoding="utf-8")
    for name in _example_names():
        stem = name.replace("SNOWFLAKE_WRITER_", "SNOWFLAKE_{p}_").replace(
            "SNOWFLAKE_READER_", "SNOWFLAKE_{p}_"
        )
        short = stem.replace("TRACEMIND_SNOWFLAKE_", "").replace("SNOWFLAKE_", "")
        assert name in source or short in source or f"SNOWFLAKE_{short}" in source, name


def test_env_example_values_are_valid_and_hold_no_secret() -> None:
    values = _example_names()
    assert values["TRACEMIND_SNOWFLAKE_ENABLED"] == "false"
    for secret in (
        "SNOWFLAKE_WRITER_PRIVATE_KEY_PASSPHRASE",
        "SNOWFLAKE_READER_PRIVATE_KEY_PASSPHRASE",
        "SNOWFLAKE_WRITER_PRIVATE_KEY_FILE",
        "SNOWFLAKE_READER_PRIVATE_KEY_FILE",
        "SNOWFLAKE_ACCOUNT",
    ):
        assert values[secret] == "", secret
    parsed = SnowflakeSettings.from_env({k: v for k, v in values.items() if v})
    assert parsed.export_min_interval_seconds == 14400 and not parsed.enabled
    assert parsed.soft_budget_credits == 15.0


def test_no_private_key_material_anywhere_in_the_repository() -> None:
    """M3-7 (source half): scan tracked-style files for key blocks. Generated environments and
    git internals are skipped."""
    skip = {
        ".git",
        ".venv",
        ".graphify-venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if any(part in skip for part in path.relative_to(ROOT).parts) or not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".gif", ".ico", ".pyc", ".zip", ".sqlite"}:
            continue
        if path.name == "test_snowflake_config.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=]{16,}", text):
            hits.append(str(path.relative_to(ROOT)))
        if re.search(r"\.(p8|pem)\b['\"]?\s*$", path.name) and path.suffix in {".p8", ".pem"}:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
