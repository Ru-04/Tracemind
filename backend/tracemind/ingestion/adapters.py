"""Adapter contract: how any telemetry source is converted into TraceMind events.

TraceMind owns its ingestion API; external platforms (OpenTelemetry exporters, Sentry, Datadog,
...) are integrated later by adding an adapter, never by changing the pipeline.

Contract (enforced for every registered adapter by tests/unit/test_adapter_contract.py):

1. `name` is a unique, lower-case identifier used as the `?format=` value.
2. `parse(body, ctx)` is a pure function of its arguments: no I/O, no clock, no global state,
   and it must not mutate `body`. The same input gives the same output.
3. `body` is already-decoded JSON of any shape. For hostile or unrecognized input the adapter
   raises `AdapterFormatError` (whole payload unusable) or yields a per-item rejection; it never
   raises anything else and never trusts the shape of the input.
4. It returns at most `ctx.max_events` items; more raises `AdapterFormatError("too_many_events")`.
5. Each emitted event is a plain dict in the `event-ingest.schema.json` shape. Adapters cannot
   skip validation: the pipeline validates, normalizes, redacts and fingerprints every event
   exactly as for native input.
6. Adapters never invent data. A value the source does not provide stays absent.
7. Rejection reasons are stable codes and never contain input values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from tracemind.errors import Rejected


class AdapterFormatError(Exception):
    """The payload as a whole is not in the adapter's format. `code` is a stable machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AdapterContext:
    project_id: str  # the authenticated project, for sources that carry no project id
    max_events: int


@dataclass(frozen=True)
class AdapterItem:
    event: dict[str, Any] | None = None
    rejection: Rejected | None = None


class EventAdapter(Protocol):
    name: ClassVar[str]

    def parse(self, body: Any, ctx: AdapterContext) -> list[AdapterItem]: ...


class AdapterRegistry:
    def __init__(self, adapters: list[EventAdapter] | None = None) -> None:
        self._adapters: dict[str, EventAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: EventAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"adapter {adapter.name} is already registered")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> EventAdapter | None:
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return sorted(self._adapters)
