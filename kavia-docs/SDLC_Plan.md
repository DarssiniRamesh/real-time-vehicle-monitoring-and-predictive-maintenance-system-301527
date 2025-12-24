# SDLC Plan (POC) – Telemetry Backend Demo Utilities

## Demo data seed + simulator

The FastAPI backend includes endpoints to generate demo assets/telemetry and to run a background simulator that produces live telemetry and occasional alerts.

### 1) Seed demo assets + initial telemetry

- Endpoint: `POST /api/v1/seed`
- Purpose: Creates demo assets (if missing) and inserts an initial batch of telemetry points spread across a lookback window.

Example:

```bash
curl -s -X POST 'http://localhost:3001/api/v1/seed' \
  -H 'Content-Type: application/json' \
  -d '{"assets": 6, "points_per_asset": 40, "lookback_minutes": 120}'
```

### 2) Start / stop the background simulator

- Start: `POST /api/v1/simulate/start`
- Stop: `POST /api/v1/simulate/stop`

Start example:

```bash
curl -s -X POST 'http://localhost:3001/api/v1/simulate/start' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Stop example:

```bash
curl -s -X POST 'http://localhost:3001/api/v1/simulate/stop'
```

### 3) Configuration (env)

Configure these in `telemetry_backend/.env` (see `.env.example`):

- `SIM_ENABLED` (default: `false`)
  - If `true`, the backend auto-starts the simulator at startup.
- `SIM_INTERVAL_SECONDS` (default: `2-5`)
  - Tick interval. Supports either a single number (`"2"`) or a jitter range (`"2-5"`).

### 4) Verify end-to-end behavior

1. Seed data:
   - `POST /api/v1/seed`
2. Start simulator:
   - `POST /api/v1/simulate/start`
3. Observe:
   - Assets: `GET /api/v1/assets`
   - Telemetry: `GET /api/v1/telemetry?assetId=...&from=...&to=...`
   - Alerts: `GET /api/v1/alerts` (spikes should produce `high` / `critical` alerts)
