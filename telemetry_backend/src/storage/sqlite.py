from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager

from datetime import datetime, timezone
from typing import Any, Iterator

from src.domain.models import Alert, AlertSeverity, AlertState, Asset, TelemetryRecord
from src.schemas.dtos import AlertFilter
from src.storage.adapter import StorageAdapter


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(dt: datetime) -> str:
    # Store as ISO-8601 UTC with timezone info to avoid ambiguity.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _str_to_dt(value: str) -> datetime:
    # datetime.fromisoformat supports offsets.
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SQLiteStorageAdapter(StorageAdapter):
    """SQLite storage implementation for the POC.

    Notes:
    - Uses sqlite3 from stdlib (no external DB container).
    - Keeps schema lightweight and easy to replace.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

        # Ensure parent directory exists (safe for relative paths too).
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        """Create required tables if they do not exist, and apply lightweight schema upgrades."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    readings_json TEXT NOT NULL,
                    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
                );
                """
            )

            # Alerts table (POC). Includes a derived `acknowledged` column for efficient filtering/indexing.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    acked_at TEXT NULL,
                    acked_by TEXT NULL,
                    ack_comment TEXT NULL,
                    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
                );
                """
            )

            # Lightweight migration: add `acknowledged` if DB was created with an older schema.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts);").fetchall()}
            if "acknowledged" not in cols:
                # SQLite supports ADD COLUMN; default value ensures existing rows are treated as unacknowledged.
                conn.execute("ALTER TABLE alerts ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0;")

                # Backfill based on state / acked_at.
                conn.execute(
                    """
                    UPDATE alerts
                    SET acknowledged = CASE
                        WHEN state = 'acked' OR acked_at IS NOT NULL THEN 1
                        ELSE 0
                    END;
                    """
                )

            # Indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_asset_ts ON telemetry(asset_id, timestamp);")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_asset_id ON alerts(asset_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged ON alerts(acknowledged);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_asset_created ON alerts(asset_id, created_at);")

    def seed_sample_assets(self) -> list[Asset]:
        """Create sample assets if the table is empty."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM assets;").fetchone()
            if row and int(row["cnt"]) > 0:
                return self.list_assets()

        # Seed a few deterministic sample assets.
        samples = [
            ("ASSET-TRUCK-001", "Truck 001", "truck"),
            ("ASSET-TRUCK-002", "Truck 002", "truck"),
            ("ASSET-EXCAV-001", "Excavator 001", "excavator"),
        ]
        created: list[Asset] = []
        for asset_id, name, type_ in samples:
            created.append(self.create_asset(asset_id=asset_id, name=name, type=type_))
        return created

    def create_asset(self, asset_id: str, name: str, type: str) -> Asset:
        now = _utc_now()
        asset = Asset(id=asset_id, name=name, type=type, created_at=now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assets (id, name, type, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (asset.id, asset.name, asset.type, _dt_to_str(asset.created_at)),
            )
        return asset

    def get_asset(self, asset_id: str) -> Asset | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assets WHERE id = ?;", (asset_id,)).fetchone()
            if not row:
                return None
            return Asset(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                created_at=_str_to_dt(row["created_at"]),
            )

    def list_assets(self) -> list[Asset]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM assets ORDER BY created_at ASC;").fetchall()
            return [
                Asset(
                    id=r["id"],
                    name=r["name"],
                    type=r["type"],
                    created_at=_str_to_dt(r["created_at"]),
                )
                for r in rows
            ]

    def insert_telemetry(self, record: TelemetryRecord) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO telemetry (asset_id, timestamp, readings_json)
                VALUES (?, ?, ?);
                """,
                (
                    record.asset_id,
                    _dt_to_str(record.timestamp),
                    json.dumps(record.readings, separators=(",", ":")),
                ),
            )
            return int(cur.lastrowid)

    def list_telemetry(
        self,
        asset_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where = ["asset_id = ?"]
        params: list[Any] = [asset_id]

        if start is not None:
            where.append("timestamp >= ?")
            params.append(_dt_to_str(start))
        if end is not None:
            where.append("timestamp <= ?")
            params.append(_dt_to_str(end))

        sql = f"""
            SELECT id, asset_id, timestamp, readings_json
            FROM telemetry
            WHERE {" AND ".join(where)}
            ORDER BY timestamp DESC
            LIMIT ?;
        """
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": int(r["id"]),
                    "asset_id": r["asset_id"],
                    "timestamp": _str_to_dt(r["timestamp"]),
                    "readings": json.loads(r["readings_json"]),
                }
            )
        return out

    def create_alert(self, asset_id: str, severity: AlertSeverity, message: str) -> Alert:
        now = _utc_now()
        alert = Alert(
            id=str(uuid.uuid4()),
            asset_id=asset_id,
            severity=severity,
            message=message,
            state=AlertState.OPEN,
            created_at=now,
            acked_at=None,
            acked_by=None,
            ack_comment=None,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alerts (
                    id, asset_id, severity, message, state, created_at,
                    acknowledged,
                    acked_at, acked_by, ack_comment
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    alert.id,
                    alert.asset_id,
                    alert.severity.value,
                    alert.message,
                    alert.state.value,
                    _dt_to_str(alert.created_at),
                    0,
                    None,
                    None,
                    None,
                ),
            )
        return alert

    def _row_to_alert(self, r: sqlite3.Row) -> Alert:
        """Convert a DB row to Alert domain model."""
        # acked_at may be NULL
        acked_at = _str_to_dt(r["acked_at"]) if r["acked_at"] else None
        state_raw = r["state"]
        state = AlertState(state_raw) if state_raw in {s.value for s in AlertState} else AlertState.OPEN

        return Alert(
            id=r["id"],
            asset_id=r["asset_id"],
            severity=AlertSeverity(r["severity"]),
            message=r["message"],
            state=state,
            created_at=_str_to_dt(r["created_at"]),
            acked_at=acked_at,
            acked_by=r["acked_by"],
            ack_comment=r["ack_comment"],
        )

    def list_alerts_filtered(self, flt: AlertFilter) -> tuple[list[Alert], int]:
        # Allowlist sorting to prevent SQL injection through column names.
        sort_map = {
            "created_at": "created_at",
            "severity": "severity",
            "asset_id": "asset_id",
        }
        sort_by = sort_map.get((flt.sort_by or "").strip(), "created_at")
        sort_dir = (flt.sort_dir or "desc").strip().lower()
        if sort_dir not in ("asc", "desc"):
            sort_dir = "desc"

        where: list[str] = []
        params: list[Any] = []

        if flt.asset_id:
            where.append("asset_id = ?")
            params.append(flt.asset_id)

        if flt.severity is not None:
            where.append("severity = ?")
            params.append(flt.severity.value)

        if flt.acknowledged is not None:
            where.append("acknowledged = ?")
            params.append(1 if flt.acknowledged else 0)

        if flt.from_ts is not None:
            where.append("created_at >= ?")
            params.append(_dt_to_str(flt.from_ts))

        if flt.to_ts is not None:
            where.append("created_at <= ?")
            params.append(_dt_to_str(flt.to_ts))

        where_sql = ""
        if where:
            where_sql = "WHERE " + " AND ".join(where)

        limit = int(flt.limit)
        offset = int(flt.offset)

        count_sql = f"SELECT COUNT(*) AS cnt FROM alerts {where_sql};"
        data_sql = f"""
            SELECT *
            FROM alerts
            {where_sql}
            ORDER BY {sort_by} {sort_dir}
            LIMIT ? OFFSET ?;
        """

        with self._connect() as conn:
            total_row = conn.execute(count_sql, tuple(params)).fetchone()
            total = int(total_row["cnt"]) if total_row else 0

            data_params = list(params) + [limit, offset]
            rows = conn.execute(data_sql, tuple(data_params)).fetchall()

        return ([self._row_to_alert(r) for r in rows], total)

    def ack_alerts(
        self,
        alert_ids: list[str],
        acked_by: str | None,
        ack_comment: str | None,
    ) -> tuple[list[Alert], list[str]]:
        if not alert_ids:
            return ([], [])

        now = _utc_now()

        placeholders = ",".join(["?"] * len(alert_ids))
        select_sql = f"SELECT * FROM alerts WHERE id IN ({placeholders});"

        with self._connect() as conn:
            rows = conn.execute(select_sql, tuple(alert_ids)).fetchall()
            found_ids = {r["id"] for r in rows}
            not_found = [i for i in alert_ids if i not in found_ids]

            # Bulk update: mark acknowledged and set ack fields.
            update_sql = f"""
                UPDATE alerts
                SET state = ?,
                    acknowledged = 1,
                    acked_at = ?,
                    acked_by = ?,
                    ack_comment = ?
                WHERE id IN ({placeholders});
            """
            conn.execute(
                update_sql,
                tuple([AlertState.ACKED.value, _dt_to_str(now), acked_by, ack_comment] + alert_ids),
            )

            # Re-fetch updated rows to return canonical values.
            updated_rows = conn.execute(select_sql, tuple([i for i in alert_ids if i in found_ids])).fetchall()

        return ([self._row_to_alert(r) for r in updated_rows], not_found)
