from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from src.domain.models import AlertSeverity, AlertState


class AssetCreateRequest(BaseModel):
    """Request DTO for creating an asset."""

    id: str = Field(..., description="Unique asset identifier (e.g., VIN or equipment ID).")
    name: str = Field(..., description="Human-readable asset name.")
    type: str = Field(..., description="Asset type/category (e.g., truck, excavator).")


class AssetResponse(BaseModel):
    """Response DTO for an asset."""

    id: str = Field(..., description="Unique asset identifier.")
    name: str = Field(..., description="Human-readable asset name.")
    type: str = Field(..., description="Asset type/category.")
    created_at: datetime = Field(..., description="UTC timestamp when the asset was created.")


class AssetStatus(str, Enum):
    """Operational status of an asset for dashboard list views (POC heuristic)."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class AssetListItem(BaseModel):
    """Response DTO for listing assets (minimal fields + status)."""

    id: str = Field(..., description="Unique asset identifier.")
    name: str = Field(..., description="Human-readable asset name.")
    type: str = Field(..., description="Asset type/category.")
    status: AssetStatus = Field(..., description="Heuristic status: active if telemetry seen recently, else inactive.")


class TelemetryIngestRequest(BaseModel):
    """Request DTO for ingesting telemetry for an asset."""

    timestamp: datetime = Field(..., description="Telemetry timestamp (UTC).")
    asset_id: str = Field(..., description="Asset identifier the telemetry belongs to.")
    readings: dict[str, Any] = Field(
        ...,
        description="Arbitrary sensor readings (key/value), e.g. {engine_temp_c: 95.2}.",
    )


class TelemetryRecordResponse(BaseModel):
    """Response DTO for telemetry records."""

    id: int = Field(..., description="Database identifier of the telemetry record.")
    timestamp: datetime = Field(..., description="Telemetry timestamp (UTC).")
    asset_id: str = Field(..., description="Asset identifier the telemetry belongs to.")
    readings: dict[str, Any] = Field(..., description="Sensor readings payload.")


class TelemetryAgg(str, Enum):
    """Aggregation function for time-series bucketing."""

    NONE = "none"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    P50 = "p50"
    P90 = "p90"


class TelemetryPoint(BaseModel):
    """A single (possibly aggregated) telemetry point for charting."""

    timestamp: datetime = Field(..., description="Point timestamp (UTC). For aggregates this is the bucket start time.")
    key: str = Field(..., description="Metric key (sensor name).")
    value: float = Field(..., description="Numeric value (raw or aggregated).")


class TelemetryQueryResponse(BaseModel):
    """Response DTO for telemetry time-series retrieval."""

    asset_id: str = Field(..., description="Asset identifier.")
    from_ts: datetime = Field(..., alias="from", description="Query window start (UTC, inclusive).")
    to_ts: datetime = Field(..., alias="to", description="Query window end (UTC, inclusive).")
    agg: TelemetryAgg = Field(..., description="Aggregation function applied.")
    interval: int | None = Field(None, ge=1, description="Bucket interval in seconds when agg != none.")
    points: list[TelemetryPoint] = Field(..., description="Flattened time-series points (timestamp, key, value).")

    model_config = {"populate_by_name": True}


class TelemetryIngestResponse(BaseModel):
    """Response DTO for telemetry ingestion results."""

    count: int = Field(..., description="Number of telemetry records inserted.")
    ids: list[int] = Field(..., description="Database ids of inserted telemetry records, in insert order.")


class AlertCreateRequest(BaseModel):
    """Request DTO for creating an alert.

    Note: In the POC this can be used by internal services/tests; later it can be produced by the ML pipeline.
    """

    asset_id: str = Field(..., description="Asset identifier the alert is associated with.")
    severity: AlertSeverity = Field(..., description="Alert severity.")
    message: str = Field(..., description="Human-readable alert message.")


class AlertDTO(BaseModel):
    """Response DTO for an alert (API-facing)."""

    id: str = Field(..., description="Alert identifier.")
    asset_id: str = Field(..., description="Asset identifier the alert is associated with.")
    severity: AlertSeverity = Field(..., description="Alert severity.")
    message: str = Field(..., description="Human-readable alert message.")
    state: AlertState = Field(..., description="Alert state.")
    created_at: datetime = Field(..., description="UTC timestamp when the alert was created.")
    acked_at: datetime | None = Field(None, description="UTC timestamp when the alert was acknowledged.")
    acked_by: str | None = Field(None, description="Identifier of user/system that acknowledged the alert.")
    ack_comment: str | None = Field(None, description="Acknowledgement comment, if provided.")


class AlertFilter(BaseModel):
    """Filter DTO for listing alerts.

    This DTO is primarily used by the service layer to keep API/query parsing separate from persistence.
    """

    asset_id: str | None = Field(None, description="Filter by asset identifier.")
    severity: AlertSeverity | None = Field(None, description="Filter by severity.")
    acknowledged: bool | None = Field(
        None,
        description="If true, return only acknowledged alerts; if false, return only unacknowledged; if null, all.",
    )
    from_ts: datetime | None = Field(None, description="Filter: created_at >= from_ts (UTC).")
    to_ts: datetime | None = Field(None, description="Filter: created_at <= to_ts (UTC).")
    sort_by: str = Field(
        "created_at",
        description="Sort column. Allowed: created_at, severity, asset_id.",
    )
    sort_dir: str = Field(
        "desc",
        description="Sort direction. Allowed: asc, desc.",
    )
    offset: int = Field(0, ge=0, description="Pagination offset (0-based).")
    limit: int = Field(50, ge=1, le=500, description="Pagination limit (max 500).")


class AlertListResponse(BaseModel):
    """Response DTO for a paginated list of alerts."""

    total: int = Field(..., ge=0, description="Total number of matching alerts (ignoring pagination).")
    items: list[AlertDTO] = Field(..., description="Alert items for this page.")


class AlertAckRequest(BaseModel):
    """Request DTO for acknowledging one or more alerts."""

    ids: list[str] = Field(..., min_length=1, description="One or more alert ids to acknowledge.")
    acked_by: str | None = Field(None, description="Optional identifier of user/system acknowledging the alert.")
    ack_comment: str | None = Field(None, description="Optional acknowledgement comment.")


class AlertAckResponse(BaseModel):
    """Response DTO for bulk acknowledgement."""

    updated: list[AlertDTO] = Field(..., description="Updated alerts that were acknowledged.")
    not_found: list[str] = Field(..., description="Alert ids that were not found.")


class AlertResponse(AlertDTO):
    """Response DTO for an alert (legacy name; prefer AlertDTO)."""


class PredictionThresholds(BaseModel):
    """DTO describing thresholds used by the rule-based inference strategy."""

    temperature_warn_c: float = Field(..., description="Temperature warning threshold in °C.")
    temperature_crit_c: float = Field(..., description="Temperature critical threshold in °C.")
    vibration_warn: float = Field(..., description="Vibration warning threshold (unit-less or g).")
    vibration_crit: float = Field(..., description="Vibration critical threshold (unit-less or g).")


class PredictionRequest(BaseModel):
    """Request DTO for baseline failure-risk prediction.

    Caller can either:
    - provide latest telemetry readings for an asset (option A), OR
    - provide asset_id to fetch recent telemetry from storage (option B).
    """

    asset_id: str | None = Field(
        None,
        description="Asset identifier. Required when `readings` is not provided (fetch from storage).",
    )
    timestamp: datetime | None = Field(
        None,
        description="Telemetry timestamp for the provided readings (UTC). Optional when fetching from storage.",
    )
    readings: dict[str, Any] | None = Field(
        None,
        description="Latest telemetry readings. If provided, inference runs directly without storage lookup.",
    )


class PredictionResult(BaseModel):
    """Response DTO for baseline risk prediction."""

    asset_id: str = Field(..., description="Asset identifier the prediction applies to.")
    risk_score: float = Field(..., ge=0.0, le=1.0, description="Failure risk score between 0 and 1.")
    severity: str = Field(..., description="Severity bucket derived from risk_score: LOW, MEDIUM, or HIGH.")
    recommendation: str = Field(..., description="Human-readable recommendation for maintenance action.")
    inferred_at: datetime = Field(..., description="UTC timestamp when the inference was performed.")
    used_timestamp: datetime | None = Field(
        None,
        description="Timestamp of telemetry used for inference (if known).",
    )


class ModelMetadata(BaseModel):
    """Response DTO describing the currently active prediction model/strategy."""

    name: str = Field(..., description="Model name identifier.")
    version: str = Field(..., description="Model version string.")
    strategy: str = Field(..., description="Inference strategy used (e.g., rule-based).")
    thresholds: PredictionThresholds = Field(..., description="Threshold configuration used for inference.")
    updated_at: datetime = Field(..., description="UTC timestamp when this model configuration was last updated.")
