"""Error types shared by the ingestion pipeline and adapters."""

from __future__ import annotations


class Rejected(Exception):  # noqa: N818 - a domain outcome, not a fault
    """One event is not acceptable. `reason` is a stable machine code; it never echoes input."""

    def __init__(self, reason: str, field: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field
