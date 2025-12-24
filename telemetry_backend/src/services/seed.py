from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from src.domain.models import TelemetryRecord
from src.storage.adapter import StorageAdapter

logger = logging.getLogger("telemetry_backend")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _base_profile_for_type(asset_type: str) -> dict[str, float]:
    """Return baseline telemetry profile for a given asset type."""
    t = (asset_type or "").lower().strip()
    if t == "excavator":
        return {"temperature_c": 78.0, "vibration": 3.2, "rpm": 1800.0, "voltage": 25.2}
    if t == "truck":
        return {"temperature_c": 82.0, "vibration": 2.6, "rpm": 2100.0, "voltage": 13.9}
    return {"temperature_c": 80.0, "vibration": 2.8, "rpm": 2000.0, "voltage": 24.0}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _make_readings(prev: dict[str, float], asset_type: str, *, spike: bool) -> dict[str, float]:
    """Generate a single sensor snapshot with light noise + occasional spike."""
    base = _base_profile_for_type(asset_type)

    # Small random walk around baseline.
    temperature_c = prev.get("temperature_c", base["temperature_c"]) + random.uniform(-0.4, 0.7)
    vibration = prev.get("vibration", base["vibration"]) + random.uniform(-0.15, 0.2)
    rpm = prev.get("rpm", base["rpm"]) + random.uniform(-40, 60)
    voltage = prev.get("voltage", base["voltage"]) + random.uniform(-0.12, 0.12)

    # Clamp to realistic ranges.
    temperature_c = _clamp(temperature_c, 55.0, 115.0)
    vibration = _clamp(vibration, 0.2, 14.0)
    rpm = _clamp(rpm, 500.0, 3200.0)
    voltage = _clamp(voltage, 11.0 if asset_type == "truck" else 20.0, 28.5)

    if spike:
        # Spikes are meant to cross prediction thresholds occasionally.
        which = random.choice(["temperature", "vibration", "both"])
        if which in ("temperature", "both"):
            temperature_c = _clamp(temperature_c + random.uniform(18.0, 32.0), 55.0, 125.0)
        if which in ("vibration", "both"):
            vibration = _clamp(vibration + random.uniform(6.0, 10.0), 0.2, 20.0)

    return {
        "temperature_c": float(round(temperature_c, 3)),
        "vibration": float(round(vibration, 3)),
        "rpm": float(round(rpm, 2)),
        "voltage": float(round(voltage, 3)),
    }


# PUBLIC_INTERFACE
def seed_demo_data(
    *,
    storage: StorageAdapter,
    assets: int,
    points_per_asset: int,
    lookback_minutes: int,
) -> tuple[list[str], int, int]:
    """Seed demo assets and insert an initial batch of telemetry.

    Args:
        storage: Storage adapter.
        assets: Number of demo assets to ensure exist (in addition to built-in samples).
        points_per_asset: Number of telemetry points to generate per asset.
        lookback_minutes: Time span to spread points across, ending at now.

    Returns:
        (asset_ids, assets_created, telemetry_inserted)
    """
    # Ensure built-in samples exist first.
    storage.seed_sample_assets()

    assets_created = 0
    demo_specs: list[tuple[str, str, str]] = []

    # Create deterministic demo ids so the frontend can rely on them.
    for i in range(1, assets + 1):
        if i % 3 == 0:
            demo_specs.append((f"ASSET-EXCAV-DEMO-{i:03d}", f"Excavator Demo {i:03d}", "excavator"))
        else:
            demo_specs.append((f"ASSET-TRUCK-DEMO-{i:03d}", f"Truck Demo {i:03d}", "truck"))

    for asset_id, name, type_ in demo_specs:
        if storage.get_asset(asset_id) is None:
            storage.create_asset(asset_id=asset_id, name=name, type=type_)
            assets_created += 1

    all_assets = storage.list_assets()
    asset_ids = [a.id for a in all_assets]

    now = _utc_now()
    start = now - timedelta(minutes=int(lookback_minutes))
    if points_per_asset <= 1:
        step = timedelta(seconds=0)
    else:
        step = (now - start) / (points_per_asset - 1)

    telemetry_inserted = 0
    state: dict[str, dict[str, float]] = {}

    for a in all_assets:
        state[a.id] = _base_profile_for_type(a.type)

    for idx in range(points_per_asset):
        ts = start + (step * idx)
        # With low probability, create a spike in this tick for some assets.
        spike_assets = set(random.sample(asset_ids, k=max(1, len(asset_ids) // 12))) if random.random() < 0.12 else set()

        for a in all_assets:
            prev = state[a.id]
            readings = _make_readings(prev, a.type, spike=(a.id in spike_assets and random.random() < 0.65))
            state[a.id] = {**prev, **readings}

            rec = TelemetryRecord(timestamp=ts, asset_id=a.id, readings=readings)
            storage.insert_telemetry(rec)
            telemetry_inserted += 1

    logger.info(
        "seed_complete",
        extra={
            "event": "seed_complete",
            "assets_created": assets_created,
            "telemetry_inserted": telemetry_inserted,
            "assets_total": len(asset_ids),
            "points_per_asset": points_per_asset,
            "lookback_minutes": lookback_minutes,
        },
    )

    return (asset_ids, assets_created, telemetry_inserted)
