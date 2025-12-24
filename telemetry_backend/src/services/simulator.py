from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.domain.models import AlertSeverity, TelemetryRecord
from src.services.prediction import predict_from_readings
from src.storage.adapter import StorageAdapter

logger = logging.getLogger("telemetry_backend")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _parse_interval_seconds(raw: str | None) -> tuple[float, float]:
    """Parse SIM_INTERVAL_SECONDS.

    Supported:
    - "2" => (2, 2)
    - "2-5" => (2, 5)

    Default: (2, 5) to introduce random jitter.
    """
    if raw is None or raw.strip() == "":
        return (2.0, 5.0)

    s = raw.strip()
    if "-" in s:
        left, right = s.split("-", 1)
        try:
            a = float(left.strip())
            b = float(right.strip())
        except ValueError:
            return (2.0, 5.0)
        lo, hi = (a, b) if a <= b else (b, a)
        lo = max(0.1, lo)
        hi = max(lo, hi)
        return (lo, hi)

    try:
        v = float(s)
        v = max(0.1, v)
        return (v, v)
    except ValueError:
        return (2.0, 5.0)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _base_profile_for_type(asset_type: str) -> dict[str, float]:
    t = (asset_type or "").lower().strip()
    if t == "excavator":
        return {"temperature_c": 78.0, "vibration": 3.2, "rpm": 1800.0, "voltage": 25.2}
    if t == "truck":
        return {"temperature_c": 82.0, "vibration": 2.6, "rpm": 2100.0, "voltage": 13.9}
    return {"temperature_c": 80.0, "vibration": 2.8, "rpm": 2000.0, "voltage": 24.0}


def _make_readings(prev: dict[str, float], asset_type: str, *, spike: bool) -> dict[str, float]:
    # Small noise / drift
    temperature_c = prev.get("temperature_c", 80.0) + random.uniform(-0.4, 0.7)
    vibration = prev.get("vibration", 2.8) + random.uniform(-0.15, 0.2)
    rpm = prev.get("rpm", 2000.0) + random.uniform(-40, 60)
    voltage = prev.get("voltage", 24.0) + random.uniform(-0.12, 0.12)

    # Realistic clamps
    temperature_c = _clamp(temperature_c, 55.0, 115.0)
    vibration = _clamp(vibration, 0.2, 14.0)
    rpm = _clamp(rpm, 500.0, 3200.0)
    voltage = _clamp(voltage, 11.0 if asset_type == "truck" else 20.0, 28.5)

    if spike:
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


def _severity_for_prediction(pred_severity: str, risk_score: float) -> AlertSeverity:
    # Map prediction service output ("LOW"/"MEDIUM"/"HIGH") to alert severities.
    sev = (pred_severity or "").strip().upper()
    if sev == "HIGH":
        # Use CRITICAL for very high risk to make it visible in UI quickly.
        return AlertSeverity.CRITICAL if risk_score >= 0.90 else AlertSeverity.HIGH
    if sev == "MEDIUM":
        return AlertSeverity.MEDIUM
    return AlertSeverity.LOW


@dataclass
class SimulatorState:
    task: asyncio.Task[None] | None = None
    stop_event: asyncio.Event | None = None
    lock: asyncio.Lock | None = None
    last_error: str | None = None


async def _sim_loop(
    *,
    storage: StorageAdapter,
    stop_event: asyncio.Event,
    interval_min: float,
    interval_max: float,
    asset_ids: list[str] | None,
) -> None:
    # Per-asset state for smooth time-series.
    local_state: dict[str, dict[str, float]] = {}

    while not stop_event.is_set():
        tick_start = _utc_now()
        try:
            assets = storage.list_assets()
            if asset_ids is not None:
                wanted = set([a.strip() for a in asset_ids if isinstance(a, str) and a.strip()])
                assets = [a for a in assets if a.id in wanted]

            # Ensure baselines exist.
            for a in assets:
                if a.id not in local_state:
                    local_state[a.id] = _base_profile_for_type(a.type)

            # Occasionally spike a subset to trigger alerts/predictions.
            spike_probability = 0.04
            spike_some = random.random() < spike_probability
            spike_set: set[str] = set()
            if spike_some and assets:
                spike_set = set(random.sample([a.id for a in assets], k=max(1, len(assets) // 4)))

            inserted = 0
            alerts_created = 0

            for a in assets:
                prev = local_state[a.id]
                readings = _make_readings(prev, a.type, spike=(a.id in spike_set and random.random() < 0.75))
                local_state[a.id] = {**prev, **readings}

                record = TelemetryRecord(timestamp=tick_start, asset_id=a.id, readings=readings)
                storage.insert_telemetry(record)
                inserted += 1

                # Trigger prediction and create alert when needed.
                pred = predict_from_readings(asset_id=a.id, readings=readings)
                alert_sev = _severity_for_prediction(pred.severity, pred.risk_score)
                if alert_sev in {AlertSeverity.CRITICAL, AlertSeverity.HIGH}:
                    msg = (
                        f"Predicted {pred.severity} risk ({pred.risk_score:.2f}). "
                        f"Temp={readings.get('temperature_c')}C Vib={readings.get('vibration')}"
                    )
                    storage.create_alert(asset_id=a.id, severity=alert_sev, message=msg)
                    alerts_created += 1

            logger.info(
                "sim_tick",
                extra={
                    "event": "sim_tick",
                    "assets": len(assets),
                    "telemetry_inserted": inserted,
                    "alerts_created": alerts_created,
                    "interval_min": interval_min,
                    "interval_max": interval_max,
                },
            )
        except Exception as exc:  # noqa: BLE001 - we want the loop to continue
            logger.exception(
                "sim_tick_error",
                extra={"event": "sim_tick_error", "error_type": type(exc).__name__},
            )

        sleep_s = random.uniform(interval_min, interval_max)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
        except TimeoutError:
            continue


# PUBLIC_INTERFACE
def get_simulation_config() -> tuple[bool, float, float]:
    """Return (enabled, interval_min, interval_max) derived from environment variables."""
    enabled = _env_truthy("SIM_ENABLED", default=False)
    interval_min, interval_max = _parse_interval_seconds(os.getenv("SIM_INTERVAL_SECONDS"))
    return (enabled, interval_min, interval_max)


# PUBLIC_INTERFACE
async def start_simulator(
    *,
    storage: StorageAdapter,
    state: SimulatorState,
    asset_ids: list[str] | None = None,
) -> tuple[bool, float, float]:
    """Start background telemetry simulation if not already running.

    Returns:
        (running, interval_min, interval_max)
    """
    enabled, interval_min, interval_max = get_simulation_config()

    # Always require explicit start call OR SIM_ENABLED=true.
    if not enabled and state.task is None:
        # still allow manual start via endpoint (even if SIM_ENABLED=false)
        pass

    if state.lock is None:
        state.lock = asyncio.Lock()

    async with state.lock:
        if state.task is not None and not state.task.done():
            return (True, interval_min, interval_max)

        stop_event = asyncio.Event()
        state.stop_event = stop_event
        state.last_error = None

        state.task = asyncio.create_task(
            _sim_loop(
                storage=storage,
                stop_event=stop_event,
                interval_min=interval_min,
                interval_max=interval_max,
                asset_ids=asset_ids,
            ),
            name="telemetry_simulator",
        )

        logger.info(
            "sim_started",
            extra={
                "event": "sim_started",
                "enabled_by_env": enabled,
                "interval_min": interval_min,
                "interval_max": interval_max,
                "asset_ids_len": len(asset_ids) if asset_ids else None,
            },
        )
        return (True, interval_min, interval_max)


# PUBLIC_INTERFACE
async def stop_simulator(*, state: SimulatorState) -> bool:
    """Stop the background telemetry simulation if running."""
    if state.lock is None:
        state.lock = asyncio.Lock()

    async with state.lock:
        if state.task is None:
            return False

        if state.stop_event is not None:
            state.stop_event.set()

        task = state.task
        state.task = None
        state.stop_event = None

    # Wait outside lock.
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except TimeoutError:
        task.cancel()
    except Exception:
        # ignore: task may already be cancelled
        pass

    logger.info("sim_stopped", extra={"event": "sim_stopped"})
    return True


# PUBLIC_INTERFACE
def get_simulator_status(state: SimulatorState) -> dict[str, Any]:
    """Return a JSON-serializable status object."""
    enabled, interval_min, interval_max = get_simulation_config()
    running = state.task is not None and not state.task.done()
    return {
        "running": running,
        "enabled_by_env": enabled,
        "interval_seconds_min": interval_min,
        "interval_seconds_max": interval_max,
    }
