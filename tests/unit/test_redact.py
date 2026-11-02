"""M1-4 (unit part): seeded fake secrets must not survive redaction.

Fake credentials are assembled at runtime so no secret-shaped literal sits in the repository."""

from typing import Any

import pytest
from tracemind.ingestion.models import validate_ingest
from tracemind.ingestion.normalize import prepare_event
from tracemind.ingestion.redact import Redactor

from tests.conftest import NOW

FAKE = {
    "bearer": "Bearer " + "abc123DEF456ghi789" * 2,
    "jwt": "eyJ"
    + "hbGciOiJIUzI1NiJ9"
    + ".eyJ"
    + "zdWIiOiIxMjM0NTY3ODkwIn0"
    + ".sig"
    + "nature12345",
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
    "ghp": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
    "groq": "gsk_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq",
    "slack": "xoxb-" + "123456789012-abcdefghijkl",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAfake\n-----END RSA PRIVATE KEY-----",
    "pw": "hunter2-" + "correct-horse",
    "email": "jane.doe" + "@example.com",
    "url": "postgres://admin:" + "s3cr3tPass" + "@db.internal:5432/app",
}


def test_each_secret_shape_is_removed() -> None:
    r = Redactor()
    samples = {
        "bearer": f"Authorization failed for header {FAKE['bearer']}",
        "jwt": f"token seen {FAKE['jwt']} in request",
        "aws": f"key {FAKE['aws']} leaked",
        "ghp": f"clone with {FAKE['ghp']} failed",
        "groq": f"using {FAKE['groq']}",
        "slack": f"hook {FAKE['slack']}",
        "pem": f"before {FAKE['pem']} after",
        "email": f"user {FAKE['email']} not found",
        "url": f"cannot connect to {FAKE['url']}",
        "pw": f"login failed password={FAKE['pw']} retry",
    }
    for name, text in samples.items():
        out, n = r.text(text)
        needle = {
            "bearer": "abc123DEF456",
            "jwt": "hbGciOiJIUzI1NiJ9",
            "aws": "IOSFODNN7EXAMPLE",
            "ghp": "a1B2c3D4e5F6",
            "groq": "Zz9Yy8Xx7Ww6",
            "slack": "abcdefghijkl",
            "pem": "MIIEowIBAAKCAQEAfake",
            "email": "jane.doe",
            "url": "s3cr3tPass",
            "pw": "hunter2",
        }[name]
        assert needle not in out, name
        assert "[REDACTED:" in out and n >= 1, name


def test_sensitive_attribute_keys_replace_whole_value_recursively() -> None:
    r = Redactor()
    out, n = r.value(
        {
            "Authorization": "anything",
            "nested": {"api_key": {"deep": "x"}, "ok": "fine"},
            "list": [{"password": "p"}, "plain"],
            "retry_count": 3,
        }
    )
    assert out["Authorization"] == "[REDACTED:key]"
    assert out["nested"]["api_key"] == "[REDACTED:key]"
    assert out["nested"]["ok"] == "fine"
    assert out["list"][0]["password"] == "[REDACTED:key]"
    assert out["retry_count"] == 3
    assert n == 3


def test_redaction_is_deterministic_and_idempotent() -> None:
    r = Redactor()
    text = f"password={FAKE['pw']} and {FAKE['bearer']} and {FAKE['email']}"
    once, n1 = r.text(text)
    twice, n2 = r.text(once)
    assert once == r.text(text)[0]
    assert twice == once and n2 == 0 and n1 >= 3


def test_email_redaction_can_be_disabled() -> None:
    out, n = Redactor(redact_emails=False).text(f"user {FAKE['email']}")
    assert FAKE["email"] in out and n == 0


def test_ordinary_text_is_left_alone() -> None:
    text = "Database connection failed after 3 retries on orders-db"
    assert Redactor().text(text) == (text, 0)


def test_whole_pipeline_removes_secrets_from_every_stored_field(project, settings) -> None:  # type: ignore[no-untyped-def]
    raw: dict[str, Any] = {
        "project_id": "project-demo",
        "service": "payments-api",
        "environment": "development",
        "timestamp": "2026-10-28T10:15:30Z",
        "severity": "ERROR",
        "message": f"login failed for {FAKE['email']} password={FAKE['pw']}",
        "error_type": "AuthError",
        "stack_trace": f'File "a.py", line 1, in f\n  url = "{FAKE["url"]}"\n  {FAKE["bearer"]}',
        "release": "1.0",
        "attributes": {"cookie": "session=zzz", "note": FAKE["jwt"], "n": [FAKE["aws"]]},
    }
    prepared = prepare_event(
        validate_ingest(raw), project, now=NOW, settings=settings, redactor=Redactor()
    )
    dumped = prepared.event.model_dump_json() + prepared.summary + prepared.event.fingerprint
    for needle in (
        "hunter2",
        "jane.doe",
        "s3cr3tPass",
        "abc123DEF456",
        "hbGciOiJ",
        "IOSFODNN7",
        "session=zzz",
    ):
        assert needle not in dumped
    assert prepared.event.redactions_applied >= 6


def test_redaction_does_not_change_grouping(project, settings, raw_event) -> None:  # type: ignore[no-untyped-def]
    a = dict(raw_event, message=f"auth failed password={FAKE['pw']}")
    b = dict(raw_event, message="auth failed password=another-different-secret")
    fa = prepare_event(validate_ingest(a), project, now=NOW, settings=settings, redactor=Redactor())
    fb = prepare_event(validate_ingest(b), project, now=NOW, settings=settings, redactor=Redactor())
    assert fa.event.fingerprint == fb.event.fingerprint


@pytest.mark.parametrize("size", [8192])
def test_redaction_of_pathological_input_is_fast(size: int) -> None:
    import time

    start = time.perf_counter()
    Redactor().text(
        "password=" + "a" * size + " " + "=" * size + " " + "-----BEGIN PRIVATE KEY-----" * 50
    )
    assert time.perf_counter() - start < 1.0
