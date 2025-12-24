from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.main import app


@pytest.fixture()
def client(tmp_path: pytest.TempPathFactory) -> TestClient:
    # Force sqlite to a temp file for isolation.
    db_file = tmp_path.mktemp("db") / "test.sqlite3"
    os.environ["SQLITE_DB_PATH"] = str(db_file)

    with TestClient(app) as c:
        yield c


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_list_assets(client: TestClient) -> None:
    resp = client.get("/api/v1/assets")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    item = data[0]
    assert set(item.keys()) >= {"id", "name", "type", "status"}
    assert item["status"] in ("active", "inactive")


def test_telemetry_raw_and_aggregated(client: TestClient) -> None:
    # Pick one seeded asset
    assets = client.get("/api/v1/assets").json()
    asset_id = assets[0]["id"]

    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    # Ingest a few telemetry records
    payload = [
        {
            "asset_id": asset_id,
            "timestamp": _iso(base + timedelta(seconds=0)),
            "readings": {"engine_temp_c": 90.0, "vibration": 4.0},
        },
        {
            "asset_id": asset_id,
            "timestamp": _iso(base + timedelta(seconds=30)),
            "readings": {"engine_temp_c": 100.0, "vibration": 6.0},
        },
        {
            "asset_id": asset_id,
            "timestamp": _iso(base + timedelta(seconds=70)),
            "readings": {"engine_temp_c": 110.0, "vibration": 9.0},
        },
    ]
    ing = client.post("/api/v1/telemetry", json=payload)
    assert ing.status_code == 200, ing.text

    from_ts = _iso(base - timedelta(seconds=5))
    to_ts = _iso(base + timedelta(seconds=120))

    # Raw
    raw = client.get(
        "/api/v1/telemetry",
        params={"assetId": asset_id, "from": from_ts, "to": to_ts, "agg": "none"},
    )
    assert raw.status_code == 200, raw.text
    raw_data = raw.json()
    assert raw_data["asset_id"] == asset_id
    assert raw_data["agg"] == "none"
    assert raw_data["interval"] is None
    assert len(raw_data["points"]) >= 6  # 3 rows * 2 numeric keys

    # Aggregated avg, 60s buckets
    agg = client.get(
        "/api/v1/telemetry",
        params={"assetId": asset_id, "from": from_ts, "to": to_ts, "agg": "avg", "interval": 60},
    )
    assert agg.status_code == 200, agg.text
    agg_data = agg.json()
    assert agg_data["agg"] == "avg"
    assert agg_data["interval"] == 60
    assert len(agg_data["points"]) >= 2  # at least one bucket, multiple keys

    # Validation: interval required when agg != none
    bad = client.get(
        "/api/v1/telemetry",
        params={"assetId": asset_id, "from": from_ts, "to": to_ts, "agg": "avg"},
    )
    assert bad.status_code == 400
