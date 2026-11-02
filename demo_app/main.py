"""FastAPI demo service. Unhandled failures emit one structured JSON log line and return a
generic 500 that never leaks details."""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from demo_app import telemetry
from demo_app.scenarios import SCENARIOS, Scenario

app = FastAPI(title="TraceMind demo service", version="0.0.0")
telemetry.configure()


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    trace_id = (
        telemetry.trace_id_from_traceparent(request.headers.get("traceparent"))
        or telemetry.new_trace_id()
    )
    telemetry.emit(
        telemetry.build_event(
            exc,
            trace_id=trace_id,
            env=os.environ,
            method=request.method,
            path=request.url.path,
        )
    )
    return JSONResponse(
        status_code=500, content={"error": "internal_error"}, headers={"X-Trace-Id": trace_id}
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/demo/scenarios")
def list_scenarios() -> list[dict[str, str]]:
    return [
        {
            "id": s.id,
            "route": s.route,
            "description": s.description,
            "expected_error_type": s.expected_error_type,
        }
        for s in SCENARIOS
    ]


def _register(scenario: Scenario) -> None:
    def endpoint() -> dict[str, str]:
        scenario.run()
        return {"status": "unexpected_success"}

    app.add_api_route(scenario.route, endpoint, methods=["GET"], name=scenario.id)


for _scenario in SCENARIOS:
    _register(_scenario)
