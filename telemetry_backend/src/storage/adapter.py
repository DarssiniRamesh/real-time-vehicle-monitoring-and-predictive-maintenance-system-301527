from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from src.domain.models import Alert, AlertSeverity, Asset, TelemetryRecord
from src.schemas.dtos import AlertFilter


class StorageAdapter(ABC):
    """Abstract storage interface for the predictive-maintenance POC.

    The goal is to allow a future replacement (e.g., Postgres, TimescaleDB, DynamoDB)
    without changing API/business logic.
    """

    # PUBLIC_INTERFACE
    @abstractmethod
    def init(self) -> None:
        """Initialize the storage backend (e.g., create tables, run migrations)."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def seed_sample_assets(self) -> list[Asset]:
        """Ensure a small set of sample assets exist and return them."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def create_asset(self, asset_id: str, name: str, type: str) -> Asset:
        """Create and return an asset."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def get_asset(self, asset_id: str) -> Asset | None:
        """Fetch a single asset by id, or None if not found."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def list_assets(self) -> list[Asset]:
        """List all assets."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def get_latest_telemetry_timestamp(self, asset_id: str) -> datetime | None:
        """Return the most recent telemetry timestamp for an asset, or None if none exists."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def query_telemetry_range(
        self,
        asset_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Query telemetry records for an asset within [start,end] (UTC) ordered by timestamp ASC.

        Returns dicts shaped like TelemetryRecordResponse fields:
        {id, timestamp, asset_id, readings}
        """

    # PUBLIC_INTERFACE
    @abstractmethod
    def insert_telemetry(self, record: TelemetryRecord) -> int:
        """Insert a telemetry record and return its database id."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def list_telemetry(
        self,
        asset_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """List telemetry records for an asset.

        Returns a list of dicts shaped like TelemetryRecordResponse fields:
        {id, timestamp, asset_id, readings}
        """

    # PUBLIC_INTERFACE
    @abstractmethod
    def create_alert(self, asset_id: str, severity: AlertSeverity, message: str) -> Alert:
        """Create and return an alert."""

    # PUBLIC_INTERFACE
    @abstractmethod
    def list_alerts_filtered(self, flt: AlertFilter) -> tuple[list[Alert], int]:
        """List alerts using filters, sorting, and pagination.

        Returns:
            (items, total_count)
        """

    # PUBLIC_INTERFACE
    @abstractmethod
    def ack_alerts(
        self,
        alert_ids: list[str],
        acked_by: str | None,
        ack_comment: str | None,
    ) -> tuple[list[Alert], list[str]]:
        """Acknowledge one or more alerts.

        Returns:
            (updated_alerts, not_found_ids)
        """

    # PUBLIC_INTERFACE
    def list_alerts(self, asset_id: str | None = None, limit: int = 200) -> list[Alert]:
        """Legacy wrapper: list alerts optionally by asset_id (no total count)."""
        flt = AlertFilter(asset_id=asset_id, limit=int(limit), offset=0)
        items, _total = self.list_alerts_filtered(flt)
        return items

    # PUBLIC_INTERFACE
    def ack_alert(self, alert_id: str, acked_by: str, ack_comment: str | None) -> Alert | None:
        """Legacy wrapper: acknowledge a single alert."""
        updated, not_found = self.ack_alerts([alert_id], acked_by=acked_by, ack_comment=ack_comment)
        if not_found:
            return None
        return updated[0] if updated else None
