"""FastAPI application factory.

Authentication here is the TEMPORARY per-project ingest key (replaced in M5). Every M1 endpoint
requires it and every query is scoped to the key's project, so one project's key can never read
or write another project's data.
"""

import json
import logging
import os
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tracemind.api.deployments import install_deployment_routes
from tracemind.api.errors import ApiError, install_error_handlers, request_id
from tracemind.api.lifecycle import install_lifecycle_routes
from tracemind.api.ratelimit import RateLimiter
from tracemind.api.repos import install_repository_routes
from tracemind.api.snowflake import install_snowflake_routes
from tracemind.config import ConfigError, Settings
from tracemind.deployments.service import DeploymentService
from tracemind.deployments.store import DeploymentStore
from tracemind.incidents.store import IncidentLifecycleStore
from tracemind.ingestion.adapters import (
    AdapterContext,
    AdapterFormatError,
    AdapterRegistry,
)
from tracemind.ingestion.native import NativeJsonAdapter
from tracemind.ingestion.otlp import OtlpLogsAdapter
from tracemind.ingestion.pipeline import IngestionPipeline
from tracemind.ingestion.redact import Redactor
from tracemind.observability.logging import LOGGER_NAME, configure_logging, log
from tracemind.projects import ConfigProjectRegistry, Project, ProjectRegistry
from tracemind.repos.graphify_runner import GraphifyRunner
from tracemind.repos.scan_service import ScanService
from tracemind.repos.scan_store import ScanStore
from tracemind.repos.service import RepositoryService
from tracemind.repos.store import RepositoryStore
from tracemind.snowflake.config import SnowflakeSettings
from tracemind.snowflake.consumption import CallLog
from tracemind.snowflake.export import Exporter
from tracemind.snowflake.queries import QueryService
from tracemind.snowflake.session import SessionFactory, UnavailableFactory
from tracemind.storage.base import EventStore, InvalidCursorError
from tracemind.storage.postgres import PostgresStore

_logger = logging.getLogger(LOGGER_NAME + ".ingest")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]  # the repository; key files must lie outside


def default_adapters() -> AdapterRegistry:
    return AdapterRegistry([NativeJsonAdapter(), OtlpLogsAdapter()])


def _reject_constant(name: str) -> Any:
    """json.loads accepts NaN/Infinity by default; they are not valid JSON and PostgreSQL's
    JSONB cannot store them."""
    raise ValueError(name)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "payload_too_large", "Request body is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "payload_too_large", "Request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    *,
    settings: Settings | None = None,
    registry: ProjectRegistry | None = None,
    store: EventStore | None = None,
    adapters: AdapterRegistry | None = None,
    repositories: RepositoryService | None = None,
    scan_service: ScanService | None = None,
    clock: Callable[[], datetime] | None = None,
    snowflake_settings: SnowflakeSettings | None = None,
    snowflake_sessions: SessionFactory | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    sf_settings = snowflake_settings or SnowflakeSettings.from_env()  # refuses a bad interval
    if registry is None:
        registry = ConfigProjectRegistry.from_file(settings.projects_file, os.environ)
    if store is None:
        if not settings.database_url:
            raise ConfigError("TRACEMIND_DATABASE_URL is not set")
        store = PostgresStore.from_url(settings.database_url)
    adapters = adapters or default_adapters()
    now: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
    pipeline = IngestionPipeline(store, settings, clock=now)
    limiter = RateLimiter(settings.rate_limit_burst, settings.rate_limit_per_second)
    db: EventStore = store

    app = FastAPI(title="TraceMind", version="0.1.0")
    install_error_handlers(app)

    def require_project(
        request: Request,
        x_tracemind_key: Annotated[str | None, Header(max_length=512)] = None,
    ) -> Project:
        project = registry.authenticate(x_tracemind_key) if x_tracemind_key else None
        if project is None:
            raise ApiError(401, "unauthorized", "Missing or invalid credentials")
        request.state.project_id = project.project_id
        return project

    Authed = Annotated[Project, Depends(require_project)]  # noqa: N806

    def throttle(project: Project, cost: int) -> None:
        wait = limiter.acquire(project.project_id, cost)
        if wait is not None:
            raise ApiError(
                429,
                "rate_limited",
                "Rate limit exceeded",
                headers={"Retry-After": str(max(1, int(wait) + 1))},
            )

    # ---- health ------------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    snowflake_health: Callable[[], str] = lambda: "disabled"  # noqa: E731

    @app.get("/ready", response_model=None)
    def ready() -> JSONResponse:
        try:
            db.ping()
        except Exception:  # noqa: BLE001 - any failure means "not ready"; details are not exposed
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "dependencies": {"database": "unavailable"}},
            )
        dependencies = {"database": "ok"}
        sf = "disabled"
        try:
            sf = snowflake_health()
        except Exception:  # noqa: BLE001 - the optional dependency must never break readiness
            sf = "unavailable"
        if sf == "disabled":
            return JSONResponse({"status": "ready", "dependencies": dependencies})
        # Snowflake is optional: a problem there is reported, never a 503. The database is
        # what readiness depends on.
        dependencies["snowflake"] = sf
        return JSONResponse(
            {"status": "ready" if sf == "ok" else "degraded", "dependencies": dependencies}
        )

    # ---- ingestion ---------------------------------------------------------------------

    @app.post("/api/v1/telemetry/events", status_code=202, response_model=None)
    async def ingest(
        request: Request,
        project: Authed,
        format: Annotated[str, Query(max_length=64)] = "tracemind-json",  # noqa: A002
    ) -> JSONResponse:
        adapter = adapters.get(format)
        if adapter is None:
            raise ApiError(400, "unknown_format", "Unknown ingestion format")
        throttle(project, 1)
        body = await _read_body(request, settings.max_body_bytes)
        try:
            data = json.loads(body, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise ApiError(400, "invalid_json", "Request body is not valid JSON") from None
        try:
            items = adapter.parse(data, AdapterContext(project.project_id, settings.max_events))
        except AdapterFormatError as exc:
            raise ApiError(422, exc.code, "Payload is not in the expected format") from None
        if len(items) > 1:
            throttle(project, len(items) - 1)
        for item in items:
            claimed = item.event.get("project_id") if item.event else None
            if isinstance(claimed, str) and claimed != project.project_id:
                raise ApiError(403, "project_mismatch", "Event project does not match credentials")

        outcomes = await run_in_threadpool(pipeline.process, project, items, source=adapter.name)
        counts = Counter(o.status for o in outcomes)
        log(
            _logger,
            logging.INFO,
            "ingest request",
            request_id=request_id(request),
            project_id=project.project_id,
            format=adapter.name,
            **{k: counts.get(k, 0) for k in ("accepted", "duplicate", "rejected")},
        )
        status = 422 if counts.get("rejected", 0) == len(outcomes) else 202
        return JSONResponse(
            status_code=status,
            content={"results": [o.to_api() for o in outcomes]},
            headers={"X-Request-ID": request_id(request)},
        )

    # ---- reads (scoped to the authenticated project) -----------------------------------

    @app.get("/api/v1/events/{event_id}")
    def get_event(event_id: str, project: Authed) -> Any:
        doc = db.get_event(project.project_id, event_id[:128])
        if doc is None:
            raise ApiError(404, "not_found", "Event not found")
        return doc

    @app.get("/api/v1/incidents")
    def list_incidents(
        project: Authed,
        service: Annotated[str | None, Query(max_length=128)] = None,
        environment: Annotated[str | None, Query(max_length=64)] = None,
        severity: Annotated[str | None, Query(pattern="^(?i:ERROR|CRITICAL)$")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> dict[str, Any]:
        try:
            items, next_cursor = db.list_incidents(
                project.project_id,
                service=service.lower() if service else None,
                environment=environment.lower() if environment else None,
                severity=severity.upper() if severity else None,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCursorError:
            raise ApiError(400, "invalid_cursor", "Invalid pagination cursor") from None
        return {"items": items, "next_cursor": next_cursor}

    @app.get("/api/v1/incidents/{incident_id}")
    def get_incident(incident_id: str, project: Authed) -> Any:
        doc = db.get_incident(project.project_id, incident_id[:128])
        if doc is None:
            raise ApiError(404, "not_found", "Incident not found")
        return doc

    @app.get("/api/v1/ingestion/health")
    def ingestion_health(
        project: Authed,
        window_hours: Annotated[int, Query(ge=1, le=168)] = 24,
    ) -> dict[str, Any]:
        return db.ingestion_health(project.project_id, window_hours=window_hours, now=now())

    if repositories is None and isinstance(store, PostgresStore):
        repositories = RepositoryService(RepositoryStore(store.engine), settings, clock=now)
    if repositories is not None:
        if scan_service is None:
            # The API only enqueues scans and reads results; the worker process runs Graphify.
            runner = (
                GraphifyRunner(
                    settings.graphify_bin,
                    home_dir=settings.data_dir / "graphify-home",
                    timeout=settings.scan_timeout_seconds,
                )
                if settings.graphify_bin
                else None
            )
            scan_service = ScanService(
                repositories=repositories,
                scans=ScanStore(repositories.store.engine),
                runner=runner,
                settings=settings,
                clock=now,
            )
        install_repository_routes(
            app,
            require_project=require_project,
            service=repositories,
            scans=scan_service,
            throttle=throttle,
        )

    if isinstance(store, PostgresStore):
        install_lifecycle_routes(
            app,
            require_project=require_project,
            store=IncidentLifecycleStore(store.engine),
            throttle=throttle,
            clock=now,
            redactor=Redactor(redact_emails=settings.redact_emails),
        )
        install_deployment_routes(
            app,
            require_project=require_project,
            service=DeploymentService(
                DeploymentStore(store.engine), repositories, settings, clock=now
            ),
            throttle=throttle,
        )
        if sf_settings.enabled:
            sf_settings.key_files_outside([_PROJECT_ROOT])
        sessions = snowflake_sessions or UnavailableFactory()
        calls = CallLog(store.engine)
        # The API process only READS export state and runs the read-only query functions. The
        # export itself runs in its own process (python -m tracemind.snowflake.export_cli).
        exporter = Exporter(store.engine, sf_settings, sessions, calls, clock=now)
        snowflake_health = exporter.health
        install_snowflake_routes(
            app,
            require_project=require_project,
            store=store,
            exporter=exporter,
            queries=QueryService(sf_settings, sessions, calls, clock=now),
            throttle=throttle,
        )

    return app
