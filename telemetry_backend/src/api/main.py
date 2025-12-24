from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from src.schemas.dtos import ModelMetadata, PredictionRequest, PredictionResult, TelemetryIngestResponse
from src.services.prediction import get_model_metadata, predict_for_asset, predict_from_readings
from src.services.telemetry_ingest import ingest_telemetry_from_csv_upload, ingest_telemetry_from_json
from src.storage.adapter import StorageAdapter
from src.storage.factory import create_storage_adapter

logger = logging.getLogger("telemetry_backend")

openapi_tags = [
    {"name": "Health", "description": "Service health and operational checks."},
    {"name": "Telemetry", "description": "Telemetry ingestion and time-series record management."},
    {"name": "Prediction", "description": "Baseline rule-based inference and model diagnostics."},
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
