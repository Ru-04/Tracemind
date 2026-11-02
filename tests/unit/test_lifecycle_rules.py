"""M3-8 (rules): the incident lifecycle table and note handling, with no database."""

from __future__ import annotations

import itertools

import pytest
from tracemind.incidents.lifecycle import (
    ALLOWED,
    MAX_NOTE,
    STATUSES,
    LifecycleError,
    check_transition,
    clean_note,
)
from tracemind.ingestion.redact import Redactor

# The owner-approved table (M3 plan 4.2), written out independently of the implementation.
EXPECTED_ALLOWED = {
    ("open", "investigating"),
    ("open", "resolved"),
    ("open", "ignored"),
    ("investigating", "open"),
    ("investigating", "resolved"),
    ("investigating", "ignored"),
    ("resolved", "open"),
    ("ignored", "open"),
}


def test_table_covers_exactly_the_approved_transitions() -> None:
    actual = {(src, dst) for src, targets in ALLOWED.items() for dst in targets}
    assert actual == EXPECTED_ALLOWED
    assert set(ALLOWED) == set(STATUSES)


@pytest.mark.parametrize(("src", "dst"), list(itertools.product(STATUSES, STATUSES)))
def test_every_pair_is_either_allowed_or_rejected_with_a_stable_code(src: str, dst: str) -> None:
    if (src, dst) in EXPECTED_ALLOWED:
        check_transition(src, dst)
        return
    with pytest.raises(LifecycleError) as caught:
        check_transition(src, dst)
    assert caught.value.code == "invalid_transition"
    assert caught.value.detail == f"{src}->{dst}"


def test_same_status_is_never_a_transition() -> None:
    for status in STATUSES:
        with pytest.raises(LifecycleError):
            check_transition(status, status)


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(LifecycleError):
        check_transition("open", "closed")
    with pytest.raises(LifecycleError):
        check_transition("deleted", "open")


def test_resolved_and_ignored_cannot_go_straight_to_other_states() -> None:
    for src in ("resolved", "ignored"):
        assert ALLOWED[src] == frozenset({"open"})


def test_ignored_requires_a_note_other_targets_do_not() -> None:
    r = Redactor()
    for blank in (None, "", "   "):
        with pytest.raises(LifecycleError) as caught:
            clean_note(blank, target="ignored", redactor=r)
        assert caught.value.code == "note_required"
        assert clean_note(blank, target="resolved", redactor=r) is None
    assert clean_note("known noisy dependency", target="ignored", redactor=r) == (
        "known noisy dependency"
    )


@pytest.mark.parametrize("bad", ["line\nbreak", "tab\there", "nul\x00", "bell\x07", "del\x7f"])
def test_control_characters_in_notes_are_rejected(bad: str) -> None:
    with pytest.raises(LifecycleError) as caught:
        clean_note(bad, target="resolved", redactor=Redactor())
    assert (caught.value.code, caught.value.detail) == ("invalid_note", "control_characters")


def test_note_length_limit_and_type() -> None:
    r = Redactor()
    assert clean_note("x" * MAX_NOTE, target="resolved", redactor=r) == "x" * MAX_NOTE
    with pytest.raises(LifecycleError) as caught:
        clean_note("x" * (MAX_NOTE + 1), target="resolved", redactor=r)
    assert caught.value.detail == "too_long"
    with pytest.raises(LifecycleError) as caught:
        clean_note(123, target="resolved", redactor=r)
    assert caught.value.detail == "not_text"


def test_notes_are_redacted_before_storage() -> None:
    out = clean_note(
        "see log: password=hunter2 from ops@example.com", target="resolved", redactor=Redactor()
    )
    assert out is not None
    assert "hunter2" not in out and "ops@example.com" not in out
    assert "[REDACTED" in out


def test_errors_carry_codes_not_submitted_text() -> None:
    with pytest.raises(LifecycleError) as caught:
        clean_note("secret\x00value", target="resolved", redactor=Redactor())
    assert "secret" not in str(caught.value) and "secret" not in (caught.value.detail or "")
