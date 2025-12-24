from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class AlertSeverity(str, Enum):
    """Severity levels for an alert."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertState(str, Enum):
    """Lifecycle state of an alert."""

    OPEN = "open"
    ACKED = "acked"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Asset:
    """Domain model representing a monitored asset (vehicle/equipment)."""

    id: str
    name: str
    type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    """Domain model representing a single telemetry record for an asset."""

    timestamp: datetime
    asset_id: str
    readings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Alert:
    """Domain model representing an alert raised for an asset."""

    id: str
    asset_id: str
    severity: AlertSeverity
    message: str
    state: AlertState
    created_at: datetime
    acked_at: datetime | None = None
    acked_by: str | None = None
    ack_comment: str | None = None
