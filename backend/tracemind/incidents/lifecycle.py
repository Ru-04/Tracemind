"""Incident lifecycle rules (M3 plan 4.2). Pure: no I/O.

`status` is lifecycle only. Moving an incident to `resolved` NEVER records a verified resolution:
`resolution.state` is a separate field that only an evidence-carrying operation may change
(planned for M4), so a human clicking "resolved" cannot make a root cause look verified.
"""

from __future__ import annotations

import re
from typing import Final

from tracemind.ingestion.redact import Redactor

STATUSES: Final = ("open", "investigating", "resolved", "ignored")

ALLOWED: Final[dict[str, frozenset[str]]] = {
    "open": frozenset({"investigating", "resolved", "ignored"}),
    "investigating": frozenset({"open", "resolved", "ignored"}),
    "resolved": frozenset({"open"}),
    "ignored": frozenset({"open"}),
}

MAX_NOTE = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LifecycleError(Exception):
    """A rejected lifecycle request. `code` is a stable identifier; `detail` is a reason code,
    never a submitted value."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def check_transition(current: str, target: str) -> None:
    """Raise `invalid_transition` unless `current -> target` is allowed. Moving to the same
    status is rejected too: a transition must change something."""
    if target not in ALLOWED.get(current, frozenset()):
        raise LifecycleError("invalid_transition", f"{current}->{target}")


def clean_note(note: object, *, target: str, redactor: Redactor) -> str | None:
    """Validate and redact a free-text note. `ignored` requires one (plan 4.2).

    Notes are redacted before storage like any other free text TraceMind keeps, because a person
    explaining a decision may paste a log line that holds a credential."""
    text: str | None = None
    if note is not None:
        if not isinstance(note, str):
            raise LifecycleError("invalid_note", "not_text")
        if _CONTROL.search(note):
            raise LifecycleError("invalid_note", "control_characters")
        text = note.strip() or None
    if text is not None and len(text) > MAX_NOTE:
        raise LifecycleError("invalid_note", "too_long")
    if text is None and target == "ignored":
        raise LifecycleError("note_required", "ignored_needs_note")
    if text is None:
        return None
    redacted, _count = redactor.text(text)
    return redacted
