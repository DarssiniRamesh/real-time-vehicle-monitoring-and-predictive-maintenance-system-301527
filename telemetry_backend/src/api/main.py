from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.schemas.dtos import (
    AlertAckRequest,
    AlertAckResponse,
    AlertFilter,
    AlertListResponse,
    APIError,
    AssetListItem,
    ErrorResponse,
    ModelMetadata,
    PredictionRequest,
    PredictionResult,
    TelemetryAgg,
    TelemetryIngestResponse,
    TelemetryQueryResponse,
)
from src.services.alerts import acknowledge_alerts, list_alerts
from src.services.prediction import get_model_metadata, predict_for_asset, predict_from_readings
from src.services.telemetry_ingest import ingest_telemetry_from_csv_upload, ingest_telemetry_from_json
from src.services.telemetry_query import get_telemetry_timeseries, list_assets_with_status
from src.storage.adapter import StorageAdapter
from src.storage.factory import create_storage_adapter

logger = logging.getLogger("telemetry_backend")

openapi_tags = [
    {"name": "Health", "description": "Service health and operational checks."},
    {"name": "Assets", "description": "Asset inventory and status listing."},
    {"name": "Telemetry", "description": "Telemetry ingestion and time-series record management."},
    {"name": "Prediction", "description": "Baseline rule-based inference and model diagnostics."},
    {"name": "Alerts", "description": "Alert lifecycle management (list, acknowledge)."},
]


def _configure_logging() -> None:
    """Configure baseline logging if the app is launched without a logging config.

    Uses simple key=value output so downstream log shippers can parse it easily.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
    )


def _parse_cors_origins(raw: str | None) -> list[str]:
    """Parse comma-separated CORS origins env var into a list."""
    if raw is None:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _init_storage(app: FastAPI) -> None:
    """Initialize embedded storage and attach it to app.state."""
    storage = create_storage_adapter()
    storage.init()
    storage.seed_sample_assets()
    app.state.storage = storage


def _get_or_create_request_id(request: Request) -> str:
    """Return request id from header if present, else generate a new one."""
    rid = request.headers.get("x-request-id")
    if rid and rid.strip():
        return rid.strip()
    return str(uuid.uuid4())


def _error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    request_id: str | None,
    details: Any | None = None,
) -> ErrorResponse:
    """Build the standardized ErrorResponse DTO."""
    return ErrorResponse(
        error=APIError(code=code, message=message, details=details),
        status_code=int(status_code),
        request_id=request_id,
    )


app = FastAPI(
    title="Telemetry & Predictive Maintenance API",
    description="POC backend for ingesting telemetry, storing time-series records, and managing maintenance alerts.",
    version="0.1.0",
    openapi_tags=openapi_tags,
)

# ---- CORS ----
# Goal: be permissive for local development (React dev server at http://localhost:3000),
# while also supporting credentialed requests. We use allow_origin_regex so that the
# middleware echoes the request Origin (instead of '*'), which works with credentials.
default_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
cors_allow_origins = _parse_cors_origins(os.getenv("CORS_ALLOW_ORIGINS")) or default_origins
cors_allow_origin_regex = os.getenv("CORS_ALLOW_ORIGIN_REGEX", ".*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_origin_regex=cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    """FastAPI startup hook: initialize DB connection and seed sample assets."""
    _configure_logging()
    _init_storage(app)
    logger.info("startup_complete", extra={"event": "startup_complete"})


# PUBLIC_INTERFACE
def get_storage(app_: FastAPI) -> StorageAdapter:
    """Get the initialized storage adapter from the FastAPI application instance."""
    storage = getattr(app_.state, "storage", None)
    if storage is None:
        # In tests or edge cases where startup isn't invoked, ensure storage exists.
        _init_storage(app_)
        storage = app_.state.storage
    return storage


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log request/response summary with a correlation id.

    - Adds/propagates X-Request-ID header.
    - Emits one log line at end of request with method/path/status/duration.
    """
    request_id = _get_or_create_request_id(request)
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        # Exception handlers will format the response; we still want a log.
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.exception(
            "request_unhandled_exception",
            extra={
                "event": "request_unhandled_exception",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            },
        )
        raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    logger.info(
        "request_complete",
        extra={
            "event": "request_complete",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client": request.client.host if request.client else None,
        },
    )

    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Return standardized error payload for HTTPException."""
    request_id = _get_or_create_request_id(request)

    details = None
    message = "Request failed."
    if isinstance(exc.detail, str):
        message = exc.detail
    else:
        # Some internal services raise dict detail (e.g., validation errors).
        details = exc.detail
        if isinstance(details, dict) and isinstance(details.get("message"), str):
            message = details["message"]

    logger.info(
        "http_exception",
        extra={
            "event": "http_exception",
            "request_id": request_id,
            "status_code": exc.status_code,
            "path": request.url.path,
            "error_detail_type": type(exc.detail).__name__,
        },
    )

    payload = _error_response(
        code="http_exception",
        message=message,
        status_code=exc.status_code,
        request_id=request_id,
        details=details,
    )
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return standardized error payload for request validation errors (422)."""
    request_id = _get_or_create_request_id(request)
    details = {"errors": exc.errors()}

    logger.info(
        "request_validation_error",
        extra={
            "event": "request_validation_error",
            "request_id": request_id,
            "path": request.url.path,
            "errors_len": len(exc.errors() or []),
        },
    )

    payload = _error_response(
        code="validation_error",
        message="Validation failed",
        status_code=422,
        request_id=request_id,
        details=details,
    )
    return JSONResponse(status_code=422, content=payload.model_dump())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return standardized error payload for unhandled exceptions (500)."""
    request_id = _get_or_create_request_id(request)

    logger.exception(
        "unhandled_exception",
        extra={
            "event": "unhandled_exception",
            "request_id": request_id,
            "path": request.url.path,
            "error_type": type(exc).__name__,
        },
    )

    payload = _error_response(
        code="internal_error",
        message="Internal server error",
        status_code=500,
        request_id=request_id,
        details={"error_type": type(exc).__name__},
    )
    return JSONResponse(status_code=500, content=payload.model_dump())


@app.get(
    "/",
    tags=["Health"],
    summary="Health Check",
    description="Simple service health check endpoint.",
    responses={200: {"description": "Service is healthy"}, 500: {"model": ErrorResponse}},
)
def health_check() -> dict:
    """Return a basic health response."""
    return {"message": "Healthy"}


@app.get(
    "/health",
    tags=["Health"],
    summary="Health Check (extended)",
    description="Extended health endpoint including API version and status.",
    operation_id="health_check_extended__get",
    responses={200: {"description": "Service is healthy"}, 500: {"model": ErrorResponse}},
)
def health_check_extended() -> dict:
    """Return health status for readiness/liveness checks."""
    return {"status": "ok", "service": "telemetry_backend", "version": app.version}


@app.get(
    "/api/v1/health",
    tags=["Health"],
    summary="API Health Check",
    description="Versioned health endpoint for clients/proxies expecting /api/v1 prefix.",
    operation_id="health_check_api_v1__get",
    responses={200: {"description": "Service is healthy"}, 500: {"model": ErrorResponse}},
)
def health_check_v1() -> dict:
    """Return health status under /api/v1 namespace."""
    return {"status": "ok"}


@app.get(
    "/api/v1/assets",
    tags=["Assets"],
    summary="List available assets",
    description="List available assets (id, name, type) with a simple ACTIVE/INACTIVE status.",
    response_model=list[AssetListItem],
    operation_id="list_assets_api_v1_assets_get",
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def list_assets(request: Request) -> list[AssetListItem]:
    """List all assets known to the system.

    Status heuristic (POC):
    - active: telemetry seen within the last 60 minutes
    - inactive: otherwise

    Sample curl:
        curl -s 'http://localhost:3001/api/v1/assets' | jq

    Returns:
        List[AssetListItem]
    """
    storage = get_storage(app)
    items = list_assets_with_status(storage)
    logger.info(
        "assets_list_success",
        extra={
            "event": "assets_list_success",
            "count": len(items),
            "content_type": request.headers.get("content-type"),
        },
    )
    return items


@app.post(
    "/api/v1/telemetry",
    tags=["Telemetry"],
    summary="Ingest telemetry records (JSON or CSV upload)",
    description=(
        "Ingest telemetry records into the embedded SQLite store.\n\n"
        "Supported content types:\n"
        "- application/json: body may be a single object or an array of objects.\n"
        "  Each object: {asset_id: str, timestamp: ISO-8601 datetime, readings|sensor: {<k>: <v>, ...}}\n"
        "- multipart/form-data: provide file field 'file' containing CSV with columns:\n"
        "  asset_id,timestamp,<sensor...>\n\n"
        "Returns the count of inserted rows and their database ids."
    ),
    response_model=TelemetryIngestResponse,
    operation_id="ingest_telemetry_api_v1_telemetry_post",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def ingest_telemetry(
    request: Request,
    payload: Any | None = Body(
        default=None,
        description="JSON telemetry payload (single object or array). Ignored when uploading a CSV file.",
    ),
    file: UploadFile | None = File(
        default=None,
        description="Optional CSV file upload. If provided, takes precedence over JSON body.",
    ),
) -> TelemetryIngestResponse:
    """Ingest telemetry records via JSON payload or CSV upload.

    - JSON: single object or list of objects.
    - CSV: multipart/form-data with a 'file' field.

    Returns: {count, ids}
    """
    storage = get_storage(app)

    if file is not None:
        result = ingest_telemetry_from_csv_upload(storage=storage, upload=file)
    else:
        if payload is None:
            raise HTTPException(status_code=400, detail="Missing request body (JSON) or file upload (CSV).")
        result = ingest_telemetry_from_json(storage=storage, payload=payload)

    logger.info(
        "telemetry_ingest_success",
        extra={
            "event": "telemetry_ingest_success",
            "count": result.count,
            "ids_len": len(result.ids),
            "content_type": request.headers.get("content-type"),
        },
    )
    return result


@app.get(
    "/api/v1/telemetry",
    tags=["Telemetry"],
    summary="Retrieve telemetry time-series (raw or aggregated)",
    description=(
        "Retrieve telemetry points for an asset within a time range.\n\n"
        "Query parameters:\n"
        "- assetId: asset identifier\n"
        "- from/to: ISO-8601 timestamps\n"
        "- agg: one of none|min|max|avg|p50|p90\n"
        "- interval: bucket size in seconds (required when agg != none)\n\n"
        "Response is a flattened list of points: (timestamp, key, value)."
    ),
    response_model=TelemetryQueryResponse,
    operation_id="get_telemetry_api_v1_telemetry_get",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def get_telemetry(
    request: Request,
    asset_id: str = Query(..., alias="assetId", description="Asset identifier."),
    from_ts: datetime = Query(..., alias="from", description="Start of time window (UTC)."),
    to_ts: datetime = Query(..., alias="to", description="End of time window (UTC)."),
    agg: TelemetryAgg = Query(
        default=TelemetryAgg.NONE,
        description="Aggregation function: none|min|max|avg|p50|p90.",
    ),
    interval: int | None = Query(
        default=None,
        ge=1,
        description="Bucket interval in seconds; required when agg is not 'none'.",
    ),
) -> TelemetryQueryResponse:
    """Retrieve raw or aggregated telemetry for charting.

    Sample curl (raw points):
        curl -s \
          'http://localhost:3001/api/v1/telemetry?assetId=ASSET-TRUCK-001&from=2025-01-01T00:00:00Z&to=2025-01-01T01:00:00Z&agg=none' | jq

    Sample curl (aggregated avg per 60s bucket):
        curl -s \
          'http://localhost:3001/api/v1/telemetry?assetId=ASSET-TRUCK-001&from=2025-01-01T00:00:00Z&to=2025-01-01T01:00:00Z&agg=avg&interval=60' | jq

    Returns:
        TelemetryQueryResponse: {asset_id, from, to, agg, interval, points}
    """
    storage = get_storage(app)
    resp = get_telemetry_timeseries(
        storage=storage,
        asset_id=asset_id,
        from_ts=from_ts,
        to_ts=to_ts,
        agg=agg,
        interval_s=interval,
    )
    logger.info(
        "telemetry_get_success",
        extra={
            "event": "telemetry_get_success",
            "asset_id": asset_id,
            "from": from_ts.isoformat(),
            "to": to_ts.isoformat(),
            "agg": agg.value,
            "interval": interval,
            "points": len(resp.points),
            "content_type": request.headers.get("content-type"),
        },
    )
    return resp


@app.post(
    "/api/v1/predict",
    tags=["Prediction"],
    summary="Baseline failure-risk prediction (rule-based)",
    description=(
        "Compute a baseline failure risk score using a simple rule-based threshold strategy.\n\n"
        "Request options:\n"
        "A) Provide `readings` (and optionally `asset_id`, `timestamp`) to run inference directly.\n"
        "B) Provide `asset_id` only to fetch the most recent telemetry from storage and infer.\n\n"
        "Returns a PredictionResult: {risk_score (0-1), severity, recommendation}."
    ),
    response_model=PredictionResult,
    operation_id="predict_api_v1_predict_post",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def predict(payload: PredictionRequest, request: Request) -> PredictionResult:
    """Run baseline rule-based inference.

    Input validation rules:
    - Must provide either `readings` OR `asset_id`.
    - If `readings` is provided, `asset_id` is required to scope the result.

    Returns:
        PredictionResult DTO.
    """
    storage = get_storage(app)

    if payload.readings is not None:
        if payload.asset_id is None or payload.asset_id.strip() == "":
            raise HTTPException(status_code=400, detail="asset_id is required when readings are provided.")
        if not isinstance(payload.readings, dict) or len(payload.readings) == 0:
            raise HTTPException(status_code=400, detail="readings must be a non-empty object.")
        result = predict_from_readings(asset_id=payload.asset_id, readings=payload.readings)
        # Preserve provided timestamp if present
        if payload.timestamp is not None:
            result.used_timestamp = payload.timestamp  # type: ignore[attr-defined]
    else:
        if payload.asset_id is None or payload.asset_id.strip() == "":
            raise HTTPException(status_code=400, detail="Provide either readings or asset_id.")
        result = predict_for_asset(storage=storage, asset_id=payload.asset_id)

    logger.info(
        "prediction_success",
        extra={
            "event": "prediction_success",
            "asset_id": result.asset_id,
            "risk_score": result.risk_score,
            "severity": result.severity,
            "content_type": request.headers.get("content-type"),
            "used_timestamp": result.used_timestamp.isoformat() if result.used_timestamp else None,
        },
    )
    return result


@app.get(
    "/api/v1/model",
    tags=["Prediction"],
    summary="Model diagnostics/metadata (rule-based)",
    description=(
        "Return metadata about the currently active prediction strategy.\n\n"
        "For this POC the strategy is deterministic and rule-based; this endpoint mirrors\n"
        "a typical ML model registry 'active model' descriptor."
    ),
    response_model=ModelMetadata,
    operation_id="model_metadata_api_v1_model_get",
    responses={500: {"model": ErrorResponse}},
)
def model_metadata() -> ModelMetadata:
    """Get current model metadata (name, version, strategy, thresholds, updated_at)."""
    return get_model_metadata()


@app.get(
    "/api/v1/alerts",
    tags=["Alerts"],
    summary="List alerts with filtering, sorting, and pagination",
    description=(
        "List alerts from storage with optional filters:\n"
        "- assetId: filter by asset\n"
        "- severity: filter by severity (critical/high/medium/low)\n"
        "- acknowledged: true/false\n"
        "- from/to: created_at time window (ISO-8601)\n\n"
        "Supports sorting and pagination and returns `total` count for the query."
    ),
    response_model=AlertListResponse,
    operation_id="list_alerts_api_v1_alerts_get",
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def get_alerts(
    request: Request,
    asset_id: str | None = Query(default=None, alias="assetId", description="Filter by asset id."),
    severity: str | None = Query(
        default=None,
        description="Filter by severity: critical, high, medium, low.",
    ),
    acknowledged: bool | None = Query(
        default=None,
        description="If true, only acknowledged alerts; if false, only unacknowledged; if omitted, all.",
    ),
    from_ts: datetime | None = Query(default=None, alias="from", description="Filter created_at >= from (UTC)."),
    to_ts: datetime | None = Query(default=None, alias="to", description="Filter created_at <= to (UTC)."),
    sort: str | None = Query(
        default=None,
        description="Sort spec: '<field>:<dir>' where field in {created_at,severity,asset_id} and dir in {asc,desc}.",
    ),
    offset: int = Query(default=0, ge=0, description="Pagination offset (0-based)."),
    limit: int = Query(default=50, ge=1, le=500, description="Pagination limit (max 500)."),
    page: int | None = Query(default=None, ge=1, description="Optional 1-based page number (overrides offset)."),
) -> AlertListResponse:
    """List alerts with filtering/sorting/pagination.

    Returns:
        AlertListResponse: {total, items}
    """
    storage = get_storage(app)

    sort_by = "created_at"
    sort_dir = "desc"
    if sort:
        raw = sort.strip()
        if ":" in raw:
            sort_by, sort_dir = [p.strip() for p in raw.split(":", 1)]
        else:
            sort_by = raw

    # If page is provided, compute offset from (page-1)*limit
    computed_offset = offset
    if page is not None:
        computed_offset = (int(page) - 1) * int(limit)

    # Normalize severity into enum via DTO validation (keeps endpoint slim)
    flt = AlertFilter(
        asset_id=asset_id,
        severity=severity,  # type: ignore[arg-type]
        acknowledged=acknowledged,
        from_ts=from_ts,
        to_ts=to_ts,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=computed_offset,
        limit=limit,
    )
    resp = list_alerts(storage=storage, flt=flt)

    logger.info(
        "alerts_list_success",
        extra={
            "event": "alerts_list_success",
            "asset_id": asset_id,
            "severity": severity,
            "acknowledged": acknowledged,
            "from": from_ts.isoformat() if from_ts else None,
            "to": to_ts.isoformat() if to_ts else None,
            "sort": sort,
            "offset": computed_offset,
            "limit": limit,
            "returned": len(resp.items),
            "total": resp.total,
            "content_type": request.headers.get("content-type"),
        },
    )
    return resp


@app.post(
    "/api/v1/alerts/ack",
    tags=["Alerts"],
    summary="Acknowledge one or more alerts",
    description=(
        "Acknowledge one or more alerts by id. Captures acknowledgement timestamp and optional user/comment.\n\n"
        "Body: {ids: [...], acked_by?: str, ack_comment?: str}"
    ),
    response_model=AlertAckResponse,
    operation_id="ack_alerts_api_v1_alerts_ack_post",
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def ack_alerts(payload: AlertAckRequest, request: Request) -> AlertAckResponse:
    """Acknowledge one or more alerts.

    Returns:
        AlertAckResponse: {updated, not_found}
    """
    storage = get_storage(app)
    resp = acknowledge_alerts(storage=storage, req=payload)

    logger.info(
        "alerts_ack_success",
        extra={
            "event": "alerts_ack_success",
            "ids_len": len(payload.ids),
            "updated_len": len(resp.updated),
            "not_found_len": len(resp.not_found),
            "acked_by": payload.acked_by,
            "content_type": request.headers.get("content-type"),
        },
    )
    return resp
