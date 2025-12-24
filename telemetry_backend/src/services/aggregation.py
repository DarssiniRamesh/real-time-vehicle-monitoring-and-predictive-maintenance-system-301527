from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor
from typing import Any, Iterable

from src.schemas.dtos import TelemetryAgg, TelemetryPoint


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bucket_start(ts: datetime, interval_s: int) -> datetime:
    """Return the bucket start datetime (UTC) for a timestamp."""
    ts_utc = _ensure_utc(ts)
    epoch = ts_utc.timestamp()
    bucket_epoch = floor(epoch / interval_s) * interval_s
    return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)


def _safe_float(v: Any) -> float | None:
    """Convert candidate numeric values to float; return None if non-numeric."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _percentile(values: list[float], p: float) -> float:
    """Compute percentile using linear interpolation between closest ranks.

    Args:
        values: Non-empty list of floats.
        p: Percentile in [0,100].

    Returns:
        Percentile value.
    """
    if not values:
        raise ValueError("values must be non-empty")
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)

    xs = sorted(values)
    # Rank in [0, n-1]
    k = (len(xs) - 1) * (p / 100.0)
    f = int(floor(k))
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    d = k - f
    return xs[f] * (1.0 - d) + xs[c] * d


@dataclass(frozen=True, slots=True)
class _AggKey:
    bucket: datetime
    key: str


# PUBLIC_INTERFACE
def aggregate_telemetry_points(
    records: Iterable[dict[str, Any]],
    agg: TelemetryAgg,
    interval_s: int,
) -> list[TelemetryPoint]:
    """Aggregate telemetry records into bucketed TelemetryPoint items.

    Input records are expected to be shaped like storage telemetry dicts:
    {id, timestamp, asset_id, readings}

    Aggregation behavior:
    - Buckets by `interval_s` (seconds) using UTC epoch bucketing.
    - Aggregates per (bucket,key) across all numeric readings in that bucket.
    - Non-numeric values are ignored for aggregation.

    Args:
        records: Telemetry rows from storage.
        agg: Aggregation function (min/max/avg/p50/p90).
        interval_s: Bucket size in seconds (>=1).

    Returns:
        List[TelemetryPoint] sorted by timestamp asc, then key asc.
    """
    if interval_s < 1:
        raise ValueError("interval_s must be >= 1")
    if agg == TelemetryAgg.NONE:
        raise ValueError("aggregate_telemetry_points requires agg != none")

    buckets: dict[_AggKey, list[float]] = {}

    for r in records:
        ts = r.get("timestamp")
        readings = r.get("readings")
        if not isinstance(ts, datetime) or not isinstance(readings, dict):
            continue

        b = _bucket_start(ts, interval_s)
        for k, v in readings.items():
            if not isinstance(k, str) or not k.strip():
                continue
            fv = _safe_float(v)
            if fv is None:
                continue
            ak = _AggKey(bucket=b, key=k)
            buckets.setdefault(ak, []).append(fv)

    points: list[TelemetryPoint] = []
    for ak, vals in buckets.items():
        if not vals:
            continue

        if agg == TelemetryAgg.MIN:
            outv = min(vals)
        elif agg == TelemetryAgg.MAX:
            outv = max(vals)
        elif agg == TelemetryAgg.AVG:
            outv = sum(vals) / float(len(vals))
        elif agg == TelemetryAgg.P50:
            outv = _percentile(vals, 50.0)
        elif agg == TelemetryAgg.P90:
            outv = _percentile(vals, 90.0)
        else:
            raise ValueError(f"Unsupported agg: {agg}")

        points.append(TelemetryPoint(timestamp=ak.bucket, key=ak.key, value=float(outv)))

    points.sort(key=lambda p: (p.timestamp, p.key))
    return points


# PUBLIC_INTERFACE
def flatten_raw_telemetry_points(records: Iterable[dict[str, Any]]) -> list[TelemetryPoint]:
    """Flatten telemetry records into raw (timestamp,key,value) points.

    Non-numeric readings are ignored to keep the time-series numeric for charting.

    Args:
        records: Telemetry rows from storage.
    """
    points: list[TelemetryPoint] = []
    for r in records:
        ts = r.get("timestamp")
        readings = r.get("readings")
        if not isinstance(ts, datetime) or not isinstance(readings, dict):
            continue
        ts_utc = _ensure_utc(ts)
        for k, v in readings.items():
            if not isinstance(k, str) or not k.strip():
                continue
            fv = _safe_float(v)
            if fv is None:
                continue
            points.append(TelemetryPoint(timestamp=ts_utc, key=k, value=float(fv)))

    points.sort(key=lambda p: (p.timestamp, p.key))
    return points
