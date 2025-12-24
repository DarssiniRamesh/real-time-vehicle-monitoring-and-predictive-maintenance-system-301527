from __future__ import annotations

from datetime import datetime
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


class AlertAckRequest(BaseModel):
    """Request DTO for acknowledging an alert."""

    acked_by: str = Field(..., description="Identifier of user/system acknowledging the alert.")
    ack_comment: str | None = Field(None, description="Optional acknowledgement comment.")


class AlertResponse(BaseModel):
    """Response DTO for an alert."""

    id: str = Field(..., description="Alert identifier.")
    asset_id: str = Field(..., description="Asset identifier the alert is associated with.")
    severity: AlertSeverity = Field(..., description="Alert severity.")
    message: str = Field(..., description="Human-readable alert message.")
    state: AlertState = Field(..., description="Alert state.")
    created_at: datetime = Field(..., description="UTC timestamp when the alert was created.")
    acked_at: datetime | None = Field(None, description="UTC timestamp when the alert was acknowledged.")
    acked_by: str | None = Field(None, description="Identifier of user/system that acknowledged the alert.")
    ack_comment: str | None = Field(None, description="Acknowledgement comment, if provided.")


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
