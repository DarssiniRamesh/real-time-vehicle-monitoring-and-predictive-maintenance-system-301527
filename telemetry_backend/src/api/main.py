from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime
from fastapi import Query

from src.schemas.dtos import (
    AlertAckRequest,
    AlertAckResponse,
    AlertFilter,
    AlertListResponse,
    ModelMetadata,
    PredictionRequest,
    PredictionResult,
    TelemetryIngestResponse,
)
from src.services.alerts import acknowledge_alerts, list_alerts
from src.services.prediction import get_model_metadata, predict_for_asset, predict_from_readings
from src.services.telemetry_ingest import ingest_telemetry_from_csv_upload, ingest_telemetry_from_json
from src.storage.adapter import StorageAdapter
from src.storage.factory import create_storage_adapter

logger = logging.getLogger("telemetry_backend")

openapi_tags = [
    {"name": "Health", "description": "Service health and operational checks."},
    {"name": "Telemetry", "description": "Telemetry ingestion and time-series record management."},
    {"name": "Prediction", "description": "Baseline rule-based inference and model diagnostics."},
    {"name": "Alerts", "description": "Alert lifecycle management (list, acknowledge)."},
]


def _init_storage(app: FastAPI) -> None:
    """Initialize embedded storage and attach it to app.state."""
    storage = create_storage_adapter()
    storage.init()
    storage.seed_sample_assets()
    app.state.storage = storage


app = FastAPI(
    title="Telemetry & Predictive Maintenance API",
    description="POC backend for ingesting telemetry, storing time-series records, and managing maintenance alerts.",
    version="0.1.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    """FastAPI startup hook: initialize DB connection and seed sample assets."""
    _init_storage(app)


# PUBLIC_INTERFACE
def get_storage(app_: FastAPI) -> StorageAdapter:
    """Get the initialized storage adapter from the FastAPI application instance."""
    storage = getattr(app_.state, "storage", None)
    if storage is None:
        # In tests or edge cases where startup isn't invoked, ensure storage exists.
        _init_storage(app_)
        storage = app_.state.storage
    return storage


@app.get("/", tags=["Health"], summary="Health Check", description="Simple service health check endpoint.")
def health_check() -> dict:
    """Return a basic health response."""
    return {"message": "Healthy"}


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

    try:
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
    except HTTPException:
        # Let explicit HTTP errors pass through.
        raise
    except Exception as exc:
        logger.exception(
            "telemetry_ingest_failed",
            extra={
                "event": "telemetry_ingest_failed",
                "content_type": request.headers.get("content-type"),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(status_code=400, detail=f"Failed to ingest telemetry: {type(exc).__name__}") from exc


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

    try:
        if payload.readings is not None:
            if payload.asset_id is None or payload.asset_id.strip() == "":
                raise HTTPException(status_code=400, detail="asset_id is required when readings are provided.")
            if not isinstance(payload.readings, dict) or len(payload.readings) == 0:
                raise HTTPException(status_code=400, detail="readings must be a non-empty object.")
            result = predict_from_readings(asset_id=payload.asset_id, readings=payload.readings)
            # preserve provided timestamp if present
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "prediction_failed",
            extra={
                "event": "prediction_failed",
                "content_type": request.headers.get("content-type"),
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(status_code=400, detail=f"Failed to run prediction: {type(exc).__name__}") from exc


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
)
def model_metadata() -> ModelMetadata:
    """Get current model metadata (name, version, strategy, thresholds, updated_at)."""
    try:
        return get_model_metadata()
    except Exception as exc:
        logger.exception(
            "model_metadata_failed",
            extra={"event": "model_metadata_failed", "error_type": type(exc).__name__},
        )
        raise HTTPException(status_code=500, detail="Failed to fetch model metadata.") from exc


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

    try:
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "alerts_list_failed",
            extra={
                "event": "alerts_list_failed",
                "error_type": type(exc).__name__,
                "asset_id": asset_id,
                "severity": severity,
                "acknowledged": acknowledged,
            },
        )
        raise HTTPException(status_code=400, detail=f"Failed to list alerts: {type(exc).__name__}") from exc


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
)
def ack_alerts(payload: AlertAckRequest, request: Request) -> AlertAckResponse:
    """Acknowledge one or more alerts.

    Returns:
        AlertAckResponse: {updated, not_found}
    """
    storage = get_storage(app)

    try:
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "alerts_ack_failed",
            extra={
                "event": "alerts_ack_failed",
                "error_type": type(exc).__name__,
                "ids_len": len(payload.ids) if payload.ids else 0,
                "content_type": request.headers.get("content-type"),
            },
        )
        raise HTTPException(status_code=400, detail=f"Failed to acknowledge alerts: {type(exc).__name__}") from exc
