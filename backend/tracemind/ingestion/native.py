"""Native TraceMind JSON adapter: `{"events": [IngestEvent, ...]}`."""

from __future__ import annotations

from typing import Any, ClassVar

from tracemind.errors import Rejected
from tracemind.ingestion.adapters import AdapterContext, AdapterFormatError, AdapterItem


class NativeJsonAdapter:
    name: ClassVar[str] = "tracemind-json"

    def parse(self, body: Any, ctx: AdapterContext) -> list[AdapterItem]:
        if not isinstance(body, dict) or set(body) != {"events"}:
            raise AdapterFormatError("invalid_envelope")
        events = body["events"]
        if not isinstance(events, list) or not events:
            raise AdapterFormatError("invalid_envelope")
        if len(events) > ctx.max_events:
            raise AdapterFormatError("too_many_events")
        return [
            AdapterItem(event=e)
            if isinstance(e, dict)
            else AdapterItem(rejection=Rejected("invalid_event"))
            for e in events
        ]
