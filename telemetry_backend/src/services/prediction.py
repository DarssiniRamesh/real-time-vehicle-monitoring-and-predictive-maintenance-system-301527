from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import HTTPException

from src.schemas.dtos import ModelMetadata, PredictionResult, PredictionThresholds
from src.storage.adapter import StorageAdapter

logger = logging.getLogger("telemetry_backend")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_float_env(name: str, default: float) -> float:
    """Parse a float env var with a safe fallback."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.info(
            "prediction_invalid_env_override",
            extra={"event": "prediction_invalid_env_override", "env": name, "value": raw},
        )
        return default


@dataclass(frozen=True, slots=True)
class _ThresholdConfig:
    temperature_warn_c: float
    temperature_crit_c: float
    vibration_warn: float
    vibration_crit: float

    def to_dto(self) -> PredictionThresholds:
        return PredictionThresholds(
            temperature_warn_c=float(self.temperature_warn_c),
            temperature_crit_c=float(self.temperature_crit_c),
            vibration_warn=float(self.vibration_warn),
            vibration_crit=float(self.vibration_crit),
        )


def _load_thresholds() -> _ThresholdConfig:
    """Load thresholds from env with sane defaults for the POC."""
    # NOTE: Orchestrator/user can override these in the container's .env.
    temp_warn = _get_float_env("PRED_TEMP_WARN_C", 95.0)
    temp_crit = _get_float_env("PRED_TEMP_CRIT_C", 105.0)
    vib_warn = _get_float_env("PRED_VIB_WARN", 7.0)
    vib_crit = _get_float_env("PRED_VIB_CRIT", 10.0)

    # Ensure monotonic thresholds.
    if temp_crit < temp_warn:
        temp_warn, temp_crit = temp_crit, temp_warn
    if vib_crit < vib_warn:
        vib_warn, vib_crit = vib_crit, vib_warn

    return _ThresholdConfig(
        temperature_warn_c=temp_warn,
        temperature_crit_c=temp_crit,
        vibration_warn=vib_warn,
        vibration_crit=vib_crit,
    )


def _first_numeric(readings: Mapping[str, Any], keys: list[str]) -> float | None:
    """Return the first numeric reading found among potential keys."""
    for k in keys:
        if k not in readings:
            continue
        v = readings.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        # attempt best-effort parse for numeric strings
        if isinstance(v, str):
            s = v.strip()
            try:
                return float(s)
            except ValueError:
                continue
    return None


def _normalize_exceedance(value: float, warn: float, crit: float) -> float:
    """Map value to [0,1] based on warn/crit thresholds."""
    if value <= warn:
        return 0.0
    if value >= crit:
        return 1.0
    if crit == warn:
        return 1.0
    return (value - warn) / (crit - warn)


def _severity_from_risk(risk: float) -> str:
    if risk >= 0.67:
        return "HIGH"
    if risk >= 0.34:
        return "MEDIUM"
    return "LOW"


def _recommendation(
    risk: float,
    temperature_c: float | None,
    vibration: float | None,
    thresholds: _ThresholdConfig,
) -> str:
    if risk < 0.34:
        return "No immediate action. Continue monitoring and review trends during routine maintenance."
    if risk < 0.67:
        return "Schedule inspection within 24-48 hours. Check cooling, lubrication, and mounting/fasteners."

    reasons: list[str] = []
    if temperature_c is not None and temperature_c >= thresholds.temperature_crit_c:
        reasons.append("temperature is critical")
    if vibration is not None and vibration >= thresholds.vibration_crit:
        reasons.append("vibration is critical")

    reason_txt = ""
    if reasons:
        reason_txt = f" Likely drivers: {', '.join(reasons)}."

    return (
        "High risk detected. Perform immediate inspection and consider taking the asset out of service if safe."
        + reason_txt
    )


# PUBLIC_INTERFACE
def predict_from_readings(asset_id: str, readings: Mapping[str, Any]) -> PredictionResult:
    """Run rule-based inference directly from provided readings.

    Args:
        asset_id: Asset identifier.
        readings: Telemetry readings map.

    Returns:
        PredictionResult DTO.
    """
    thresholds = _load_thresholds()

    temperature_c = _first_numeric(
        readings,
        keys=[
            "temperature",
            "temp",
            "engine_temp",
            "engine_temp_c",
            "engine_temperature_c",
            "coolant_temp_c",
        ],
    )
    vibration = _first_numeric(
        readings,
        keys=[
            "vibration",
            "vibration_rms",
            "vibration_g",
            "vibration_mm_s",
        ],
    )

    # Base exceedances. If one sensor missing, it contributes 0.
    temp_r = (
        _normalize_exceedance(temperature_c, thresholds.temperature_warn_c, thresholds.temperature_crit_c)
        if temperature_c is not None
        else 0.0
    )
    vib_r = (
        _normalize_exceedance(vibration, thresholds.vibration_warn, thresholds.vibration_crit)
        if vibration is not None
        else 0.0
    )

    # Simple fusion: max signal, with a small boost when both are elevated.
    risk = max(temp_r, vib_r)
    if temp_r >= 0.5 and vib_r >= 0.5:
        risk = min(1.0, risk + 0.15)

    severity = _severity_from_risk(risk)
    rec = _recommendation(risk=risk, temperature_c=temperature_c, vibration=vibration, thresholds=thresholds)

    return PredictionResult(
        asset_id=asset_id,
        risk_score=float(round(risk, 4)),
        severity=severity,
        recommendation=rec,
        inferred_at=_utc_now(),
        used_timestamp=None,
    )


# PUBLIC_INTERFACE
def predict_for_asset(storage: StorageAdapter, asset_id: str) -> PredictionResult:
    """Fetch recent telemetry from storage and run inference.

    Uses the most recent telemetry record available (by timestamp DESC).

    Raises:
        HTTPException(404): unknown asset_id or no telemetry found.
    """
    if storage.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown asset_id: {asset_id}")

    rows = storage.list_telemetry(asset_id=asset_id, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No telemetry found for asset_id: {asset_id}")

    latest = rows[0]
    readings = latest.get("readings")
    if not isinstance(readings, dict):
        raise HTTPException(status_code=400, detail="Stored telemetry record has invalid readings format.")

    result = predict_from_readings(asset_id=asset_id, readings=readings)
    # Preserve timestamp used for inference when possible.
    used_ts = latest.get("timestamp")
    if isinstance(used_ts, datetime):
        result.used_timestamp = used_ts  # type: ignore[attr-defined]
    return result


# PUBLIC_INTERFACE
def get_model_metadata() -> ModelMetadata:
    """Return current rule-based 'model' metadata.

    This mirrors typical ML model registry metadata but for a deterministic threshold strategy.
    """
    thresholds = _load_thresholds()
    name = os.getenv("PRED_MODEL_NAME", "baseline-rule-thresholds")
    version = os.getenv("PRED_MODEL_VERSION", "1.0.0")
    return ModelMetadata(
        name=name,
        version=version,
        strategy="rule-based",
        thresholds=thresholds.to_dto(),
        updated_at=_utc_now(),
    )
