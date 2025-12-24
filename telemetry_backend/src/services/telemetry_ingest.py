from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from src.domain.models import TelemetryRecord
from src.schemas.dtos import TelemetryIngestRequest, TelemetryIngestResponse
from src.storage.adapter import StorageAdapter

logger = logging.getLogger("telemetry_backend")


def _coerce_timestamp(value: Any) -> Any:
    """Best-effort coercion of CSV timestamps to datetime.

    JSON parsing uses Pydantic directly, but CSV values arrive as strings.
    """
    if isinstance(value, datetime):
        return value
    if value is None:
        return value
    if not isinstance(value, str):
        return value

    # Allow ISO-8601 strings with Z or offsets; if naive, assume UTC.
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_json_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize incoming JSON telemetry object to our DTO shape.

    The request spec mentions a 'sensor map'; internally we use `readings`.
    Accept both keys:
    - readings: {...}
    - sensor: {...}  (alias)
    """
    data = dict(item)

    if "readings" not in data and "sensor" in data:
        data["readings"] = data.pop("sensor")

    return data


def _ensure_asset_exists(storage: StorageAdapter, asset_id: str) -> None:
    if storage.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown asset_id: {asset_id}")


def _insert_many(storage: StorageAdapter, records: Iterable[TelemetryIngestRequest]) -> TelemetryIngestResponse:
    ids: list[int] = []
    for dto in records:
        _ensure_asset_exists(storage, dto.asset_id)
        record = TelemetryRecord(timestamp=dto.timestamp, asset_id=dto.asset_id, readings=dto.readings)
        ids.append(storage.insert_telemetry(record))
    return TelemetryIngestResponse(count=len(ids), ids=ids)


# PUBLIC_INTERFACE
def ingest_telemetry_from_json(storage: StorageAdapter, payload: Any) -> TelemetryIngestResponse:
    """Validate and persist telemetry from a JSON payload.

    Args:
        storage: Storage adapter instance.
        payload: Either a single telemetry object or a list of telemetry objects.

    Returns:
        TelemetryIngestResponse containing count and inserted ids.

    Raises:
        HTTPException(400): For invalid payload shape or validation errors.
        HTTPException(404): If asset_id does not exist.
    """
    try:
        items: Sequence[Any]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            raise HTTPException(status_code=400, detail="JSON payload must be an object or an array of objects.")

        dtos: list[TelemetryIngestRequest] = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail=f"Telemetry item at index {i} must be an object.")
            normalized = _normalize_json_item(raw)
            dtos.append(TelemetryIngestRequest.model_validate(normalized))

        return _insert_many(storage, dtos)
    except ValidationError as ve:
        logger.info(
            "telemetry_validation_error",
            extra={"event": "telemetry_validation_error", "error": ve.errors()},
        )
        raise HTTPException(status_code=400, detail={"message": "Validation failed", "errors": ve.errors()}) from ve


def _parse_csv_rows(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV missing header row.")
    if "asset_id" not in reader.fieldnames or "timestamp" not in reader.fieldnames:
        raise HTTPException(
            status_code=400,
            detail="CSV must include columns: asset_id,timestamp,<sensor...>",
        )

    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(reader):
        asset_id = (row.get("asset_id") or "").strip()
        timestamp_raw = row.get("timestamp")

        if not asset_id:
            raise HTTPException(status_code=400, detail=f"CSV row {idx+1}: asset_id is required.")
        if timestamp_raw is None or str(timestamp_raw).strip() == "":
            raise HTTPException(status_code=400, detail=f"CSV row {idx+1}: timestamp is required.")

        readings: dict[str, Any] = {}
        for k, v in row.items():
            if k in ("asset_id", "timestamp"):
                continue
            if k is None:
                continue
            key = str(k).strip()
            if not key:
                continue
            if v is None:
                continue
            sval = str(v).strip()
            if sval == "":
                continue

            # Try numeric conversion; fallback to string.
            try:
                if "." in sval:
                    readings[key] = float(sval)
                else:
                    readings[key] = int(sval)
            except ValueError:
                readings[key] = sval

        rows.append(
            {
                "asset_id": asset_id,
                "timestamp": _coerce_timestamp(timestamp_raw),
                "readings": readings,
            }
        )

    return rows


# PUBLIC_INTERFACE
def ingest_telemetry_from_csv_upload(storage: StorageAdapter, upload: UploadFile) -> TelemetryIngestResponse:
    """Validate and persist telemetry from a CSV UploadFile.

    The CSV must include headers: asset_id,timestamp,<sensor...>

    Args:
        storage: Storage adapter instance.
        upload: FastAPI UploadFile containing CSV content.

    Returns:
        TelemetryIngestResponse containing count and inserted ids.

    Raises:
        HTTPException(400): For invalid CSV or validation errors.
        HTTPException(404): If asset_id does not exist.
    """
    filename = upload.filename or "upload.csv"
    content_type = upload.content_type or ""

    if "csv" not in content_type and not filename.lower().endswith(".csv"):
        # Best-effort check; still accept if user uploads with generic content-type.
        logger.info(
            "telemetry_csv_suspicious_content_type",
            extra={"event": "telemetry_csv_suspicious_content_type", "content_type": content_type, "filename": filename},
        )

    raw = upload.file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc

    try:
        rows = _parse_csv_rows(text)
        dtos = [TelemetryIngestRequest.model_validate(r) for r in rows]
        return _insert_many(storage, dtos)
    except ValidationError as ve:
        logger.info(
            "telemetry_validation_error",
            extra={"event": "telemetry_validation_error", "error": ve.errors(), "filename": filename},
        )
        raise HTTPException(status_code=400, detail={"message": "Validation failed", "errors": ve.errors()}) from ve
