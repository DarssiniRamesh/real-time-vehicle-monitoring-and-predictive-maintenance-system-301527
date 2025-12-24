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
