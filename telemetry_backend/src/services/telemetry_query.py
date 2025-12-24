from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.schemas.dtos import (
    AssetListItem,
    AssetStatus,
    TelemetryAgg,
    TelemetryPoint,
    TelemetryQueryResponse,
)
from src.services.aggregation import aggregate_telemetry_points, flatten_raw_telemetry_points
from src.storage.adapter import StorageAdapter

logger = logging.getLogger("telemetry_backend")


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# PUBLIC_INTERFACE
def list_assets_with_status(storage: StorageAdapter) -> list[AssetListItem]:
    """List assets including a simple ACTIVE/INACTIVE status.

    POC heuristic:
    - ACTIVE if the asset has telemetry within the last 60 minutes.
    - INACTIVE otherwise.

    This keeps the endpoint cheap and makes the frontend list meaningful without
    implementing full fleet state machinery.
    """
    assets = storage.list_assets()
    cutoff = _utc_now() - timedelta(minutes=60)

    out: list[AssetListItem] = []
    for a in assets:
        last_ts = storage.get_latest_telemetry_timestamp(a.id)
        status = AssetStatus.INACTIVE
        if last_ts is not None and _ensure_utc(last_ts) >= cutoff:
            status = AssetStatus.ACTIVE

        out.append(AssetListItem(id=a.id, name=a.name, type=a.type, status=status))
    return out


# PUBLIC_INTERFACE
def get_telemetry_timeseries(
    storage: StorageAdapter,
    asset_id: str,
    from_ts: datetime,
    to_ts: datetime,
    agg: TelemetryAgg,
    interval_s: int | None,
) -> TelemetryQueryResponse:
    """Retrieve raw or aggregated telemetry points for an asset.

    Args:
        storage: Storage adapter.
        asset_id: Asset identifier.
        from_ts: Start datetime (UTC).
        to_ts: End datetime (UTC).
        agg: Aggregation function.
        interval_s: Bucket interval seconds (required when agg != none).

    Returns:
        TelemetryQueryResponse
    """
    if storage.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown asset_id: {asset_id}")

    f_utc = _ensure_utc(from_ts)
    t_utc = _ensure_utc(to_ts)
    if t_utc < f_utc:
        raise HTTPException(status_code=400, detail="Invalid time range: to must be >= from")

    if agg != TelemetryAgg.NONE:
        if interval_s is None:
            raise HTTPException(status_code=400, detail="interval is required when agg is not 'none'")
        if int(interval_s) < 1:
            raise HTTPException(status_code=400, detail="interval must be >= 1 second")
        if (t_utc - f_utc).total_seconds() > 60 * 60 * 24 * 31:
            # Prevent runaway POC queries.
            raise HTTPException(status_code=400, detail="Time range too large for aggregated query (max 31 days)")

    # Query storage once; apply aggregation in Python for POC.
    rows = storage.query_telemetry_range(asset_id=asset_id, start=f_utc, end=t_utc)

    points: list[TelemetryPoint]
    if agg == TelemetryAgg.NONE:
        points = flatten_raw_telemetry_points(rows)
    else:
        points = aggregate_telemetry_points(rows, agg=agg, interval_s=int(interval_s or 0))

    logger.info(
        "telemetry_query_success",
        extra={
            "event": "telemetry_query_success",
            "asset_id": asset_id,
            "from": f_utc.isoformat(),
            "to": t_utc.isoformat(),
            "agg": agg.value,
            "interval": interval_s,
            "records": len(rows),
            "points": len(points),
        },
    )

    return TelemetryQueryResponse(
        asset_id=asset_id,
        **{"from": f_utc, "to": t_utc},
        agg=agg,
        interval=interval_s,
        points=points,
    )
