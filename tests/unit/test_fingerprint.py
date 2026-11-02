"""M1-6: fingerprints are deterministic, stable across noise, and locked by golden values."""

from typing import Any

import pytest
from tracemind.fingerprint import (
    compute_fingerprint,
    normalize_message,
    signature,
    top_application_frame,
)

TRACE_A = (
    "Traceback (most recent call last):\n"
    '  File "C:\\Users\\alice\\proj\\demo_app\\scenarios.py", line 30, in regression\n'
    "    return average_item_price(total_cents=0, quantity=0)\n"
    '  File "C:\\Users\\alice\\proj\\demo_app\\scenarios.py", line 25, in average_item_price\n'
    "    return total_cents // quantity\n"
    "ZeroDivisionError: integer division or modulo by zero"
)
# Same failure on another machine, different OS paths and line numbers.
TRACE_B = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/demo_app/scenarios.py", line 99, in regression\n'
    '  File "/srv/app/demo_app/scenarios.py", line 77, in average_item_price\n'
    "ZeroDivisionError: integer division or modulo by zero"
)


def fp(**kw: object) -> str:
    base: dict[str, Any] = dict(
        project_id="project-demo",
        service="demo-payments",
        environment="development",
        error_type="ZeroDivisionError",
        message="integer division or modulo by zero",
        stack_trace=None,
    )
    base.update(kw)
    return compute_fingerprint(**base)


def test_golden_value_locks_v1() -> None:
    # sha256 of the literal string from docs/contracts/fingerprint.md, computed independently:
    # "v1|project-demo|demo-payments|development|ZeroDivisionError|
    #  msg:integer division or modulo by zero" (joined without the line break)
    assert fp() == "a2c1e24d3af3680446845bee384c2501e4397d373ad61a3497f21760af08b7f0"


def test_same_inputs_same_fingerprint() -> None:
    assert fp() == fp()


def test_frame_signature_ignores_os_paths_and_line_numbers() -> None:
    assert fp(stack_trace=TRACE_A) == fp(stack_trace=TRACE_B)
    assert top_application_frame(TRACE_A) == "demo_app/scenarios.py:average_item_price"


def test_message_volatile_values_do_not_split_groups() -> None:
    a = fp(error_type="E", message='User 1234 failed id=9f8b7c6d5e4f order "A-1" at 2.5s')
    b = fp(error_type="E", message='User 99 failed id=11223344aabb order "Z-9" at 70s')
    assert a == b
    assert normalize_message("id 123e4567-e89b-12d3-a456-426614174000") == "id <uuid>"


def test_different_wording_is_not_merged() -> None:
    assert fp(message="Connection refused") != fp(message="Connection reset")


@pytest.mark.parametrize(
    "patch",
    [
        {"service": "other"},
        {"environment": "production"},
        {"error_type": "OtherError"},
        {"project_id": "p2"},
    ],
)
def test_scope_fields_change_the_fingerprint(patch: dict[str, str]) -> None:
    assert fp(**patch) != fp()


def test_missing_error_type_is_handled() -> None:
    assert fp(error_type=None) != fp(error_type="-X")
    assert len(fp(error_type=None)) == 64


def test_frame_beats_message() -> None:
    assert fp(stack_trace=TRACE_A, message="one") == fp(stack_trace=TRACE_A, message="two")
    assert signature("m", None).startswith("msg:")
    assert signature("m", TRACE_A).startswith("frame:")


def test_library_frames_are_skipped() -> None:
    trace = (
        '  File "/srv/app/svc/handler.py", line 10, in handle\n'
        '  File "/venv/lib/python3.12/site-packages/httpx/_client.py", line 5, in send\n'
        '  File "/usr/lib/python3.12/socket.py", line 1, in connect\n'
    )
    assert top_application_frame(trace) == "svc/handler.py:handle"


def test_redaction_markers_do_not_alter_normalization() -> None:
    assert (
        normalize_message("bad token [REDACTED:token] for 7")
        == "bad token [REDACTED:token] for <num>"
    )
