from __future__ import annotations

import os

from src.storage.adapter import StorageAdapter
from src.storage.sqlite import SQLiteStorageAdapter


# PUBLIC_INTERFACE
def create_storage_adapter() -> StorageAdapter:
    """Create and return the configured StorageAdapter implementation.

    Environment variables:
    - SQLITE_DB_PATH: path to SQLite database file (default: ./data/telemetry_poc.sqlite3)

    Note: The orchestrator/user should set env vars in the container's .env file (do not hardcode secrets here).
    """
    db_path = os.getenv("SQLITE_DB_PATH", "./data/telemetry_poc.sqlite3")
    return SQLiteStorageAdapter(db_path=db_path)
