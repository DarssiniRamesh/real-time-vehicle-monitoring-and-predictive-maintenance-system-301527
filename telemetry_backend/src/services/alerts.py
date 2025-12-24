from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException

from src.schemas.dtos import AlertAckRequest, AlertAckResponse, AlertDTO, AlertFilter, AlertListResponse
from src.storage.adapter import StorageAdapter

logger = logging.getLogger("telemetry_backend")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _alert_to_dto(a) -> AlertDTO:
    return AlertDTO(
        id=a.id,
        asset_id=a.asset_id,
        severity=a.severity,
        message=a.message,
        state=a.state,
        created_at=a.created_at,
        acked_at=a.acked_at,
        acked_by=a.acked_by,
        ack_comment=a.ack_comment,
    )


# PUBLIC_INTERFACE
def list_alerts(storage: StorageAdapter, flt: AlertFilter) -> AlertListResponse:
    """List alerts with filtering, sorting, and pagination.

    Args:
        storage: Storage adapter implementation.
        flt: AlertFilter describing query behavior.

    Returns:
        AlertListResponse with total count and items.

    Raises:
        HTTPException(400): for invalid filter configuration.
    """
    # Defensive validation (keep unit-testable & consistent across adapters)
    allowed_sort = {"created_at", "severity", "asset_id"}
    if flt.sort_by not in allowed_sort:
        raise HTTPException(status_code=400, detail=f"Invalid sort_by. Allowed: {sorted(allowed_sort)}")

    if flt.sort_dir.lower() not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="Invalid sort_dir. Allowed: asc, desc")

    if flt.limit < 1 or flt.limit > 500:
        raise HTTPException(status_code=400, detail="Invalid limit. Must be between 1 and 500.")

    if flt.offset < 0:
        raise HTTPException(status_code=400, detail="Invalid offset. Must be >= 0.")

    items, total = storage.list_alerts_filtered(flt)
    return AlertListResponse(total=total, items=[_alert_to_dto(a) for a in items])


@dataclass(frozen=True, slots=True)
class _AckParams:
    acked_by: str | None
    ack_comment: str | None
    acked_at: datetime


# PUBLIC_INTERFACE
def acknowledge_alerts(storage: StorageAdapter, req: AlertAckRequest) -> AlertAckResponse:
    """Acknowledge one or more alerts by id.

    Captures acknowledgement timestamp in the persistence layer (adapter records UTC now).

    Args:
        storage: Storage adapter implementation.
        req: AlertAckRequest with ids and optional ack fields.

    Returns:
        AlertAckResponse including updated alerts and ids not found.

    Raises:
        HTTPException(400): for invalid request.
    """
    ids = [i.strip() for i in (req.ids or []) if isinstance(i, str) and i.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="ids must contain at least one non-empty alert id.")

    # Optional fields: normalize empty strings to None
    acked_by = req.acked_by.strip() if isinstance(req.acked_by, str) and req.acked_by.strip() else None
    ack_comment = req.ack_comment.strip() if isinstance(req.ack_comment, str) and req.ack_comment.strip() else None

    updated, not_found = storage.ack_alerts(ids, acked_by=acked_by, ack_comment=ack_comment)
    return AlertAckResponse(updated=[_alert_to_dto(a) for a in updated], not_found=not_found)
