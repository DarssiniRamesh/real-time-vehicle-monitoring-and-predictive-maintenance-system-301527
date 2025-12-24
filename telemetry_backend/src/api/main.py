from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.storage.adapter import StorageAdapter
from src.storage.factory import create_storage_adapter

openapi_tags = [
    {"name": "Health", "description": "Service health and operational checks."},
]


def _init_storage(app: FastAPI) -> None:
    """Initialize embedded storage and attach it to app.state."""
    storage = create_storage_adapter()
    storage.init()
    storage.seed_sample_assets()
    app.state.storage = storage


app = FastAPI(
    title="Telemetry & Predictive Maintenance API",
    description="POC backend for ingesting telemetry, storing time-series records, and managing maintenance alerts.",
    version="0.1.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    """FastAPI startup hook: initialize DB connection and seed sample assets."""
    _init_storage(app)


# PUBLIC_INTERFACE
def get_storage(app_: FastAPI) -> StorageAdapter:
    """Get the initialized storage adapter from the FastAPI application instance."""
    storage = getattr(app_.state, "storage", None)
    if storage is None:
        # In tests or edge cases where startup isn't invoked, ensure storage exists.
        _init_storage(app_)
        storage = app_.state.storage
    return storage


@app.get("/", tags=["Health"], summary="Health Check", description="Simple service health check endpoint.")
def health_check() -> dict:
    """Return a basic health response."""
    return {"message": "Healthy"}
