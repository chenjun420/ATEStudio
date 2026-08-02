# ATE Studio API Reference

Base URL: `http://localhost:8000`
All routes are prefixed with `/api/v1` (configured in `main.py`).
Interactive Swagger docs: `http://localhost:8000/docs`

---

## Wired Routers (10 routers, 46 endpoints)

These routers are registered in `src/ate_cloud/api/v1/router.py` and active on the running server.

---

### Health (no prefix)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/health.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health/db` | Check database connectivity |

Returns `{"status": "healthy"}` on success, `503` on failure.

---

### Node Templates (prefix: `/node-templates`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/node_templates.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/node-templates` | List all node templates with pagination |
| GET | `/api/v1/node-templates/{template_id}` | Get a specific node template by ID |
| POST | `/api/v1/node-templates` | Create a new node template |
| PUT | `/api/v1/node-templates/{template_id}` | Update an existing node template |
| DELETE | `/api/v1/node-templates/{template_id}` | Delete a node template |

Manages graph editor node templates (appearance, default data, type).

---

### Scripts (prefix: `/scripts`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/scripts.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/scripts` | List all scripts with pagination |
| GET | `/api/v1/scripts/{script_id}` | Get script metadata by ID |
| POST | `/api/v1/scripts` | Upload a new script |
| PUT | `/api/v1/scripts/{script_id}` | Update script metadata |
| DELETE | `/api/v1/scripts/{script_id}` | Delete a script |
| GET | `/api/v1/scripts/{script_id}/content` | Read script file content from disk |
| PUT | `/api/v1/scripts/{script_id}/content` | Write script file content (creates Git commit) |
| GET | `/api/v1/scripts/{script_id}/versions` | List Git version history for a script |
| GET | `/api/v1/scripts/{script_id}/versions/{commit_hash}` | Read script content at a specific Git commit |

Scripts are stored with metadata in the database and file content in a Git-backed storage. The versioning endpoints require the `ScriptVersioningService` to be initialized on `app.state`.

---

### Script Generation (prefix: `/scripts`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/scripts_generate.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/scripts/generate` | AI-generate a test script from natural language specification |
| POST | `/api/v1/scripts/refine` | AI-refine an existing script based on user feedback |

Requires `OPENAI_API_KEY` to be configured. Uses a circuit breaker to handle LLM failures gracefully. The generation pipeline includes AST validation, security scanning, and dependency checking.

---

### Sequences (prefix: `/sequences`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/sequences.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/sequences` | List all test sequences with pagination |
| GET | `/api/v1/sequences/{sequence_id}` | Get a specific sequence by ID |
| POST | `/api/v1/sequences` | Create a new sequence |
| PUT | `/api/v1/sequences/{sequence_id}` | Update an existing sequence |
| DELETE | `/api/v1/sequences/{sequence_id}` | Delete a sequence |

Sequences are YAML-based test plans. Returns `409` if a sequence name already exists.

---

### Executions (prefix: `/executions`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/executions.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/executions` | Start a new execution (creates PENDING record) |
| GET | `/api/v1/executions` | List executions with pagination (ordered by created_at DESC) |
| POST | `/api/v1/executions/search` | Search executions with advanced filters (serial, product_type, status, date range) |
| GET | `/api/v1/executions/{run_id}` | Get execution details by run ID |
| POST | `/api/v1/executions/{run_id}/abort` | Abort a running execution |
| GET | `/api/v1/executions/{run_id}/events` | SSE stream of real-time execution events |

The `POST /search` endpoint supports AND-combined filters: `serial_number` (partial match), `product_type` (exact), `status` (exact), `date_from`/`date_to` (range on `started_at`). Pagination via `skip`/`limit` (max 500).

---

### Changeover (prefix: `/changeover`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/changeover.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/changeover/matrix` | Get the full product changeover cost matrix |
| GET | `/api/v1/changeover/products` | List all known product types in the changeover system |
| PUT | `/api/v1/changeover/{product_a}/{product_b}` | Register or update a transition cost between products |
| DELETE | `/api/v1/changeover/{product_a}/{product_b}` | Remove a registered transition cost |
| POST | `/api/v1/changeover/optimize` | Optimize a product sequence to minimize total changeover cost |

The changeover matrix is stored in-memory (no DB table). The optimizer uses a CP-SAT solver from Google OR-Tools.

---

### Dashboard (prefix: `/dashboard`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/dashboard.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/dashboard/summary` | Aggregated production overview (active workers, today's executions, pass rate, fault count) |
| GET | `/api/v1/dashboard/stations` | Per-station (worker) status list with online/offline, capabilities, task load |
| GET | `/api/v1/dashboard/faults` | Fault rate trend (24h hourly buckets) and Top-5 Pareto by error category |
| GET | `/api/v1/dashboard/executions` | Today's execution breakdown by status with recent 10 entries |

Data sources: execution records (SQLAlchemy), active workers (NATS JetStream KV), fault records (Qdrant). All Qdrant/NATS dependencies degrade gracefully to empty/zero when unavailable.

---

### Resources (prefix: `/resources`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/resources.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/resources/humans` | Register a new human operator |
| GET | `/api/v1/resources/humans` | List all registered human operators |
| GET | `/api/v1/resources/humans/{operator_id}` | Get a single operator by ID |
| DELETE | `/api/v1/resources/humans/{operator_id}` | Remove a registered operator |
| POST | `/api/v1/resources/robots` | Register a new robot workstation |
| GET | `/api/v1/resources/robots` | List all registered robot workstations |
| GET | `/api/v1/resources/robots/{robot_id}` | Get a single robot by ID |
| DELETE | `/api/v1/resources/robots/{robot_id}` | Remove a registered robot |

Resources are stored in-memory (module-level dicts, same pattern as changeover). Used by the CP-SAT scheduler for resource-constrained scheduling.

---

### Reports (prefix: `/reports`)

**Status**: ✅ Wired
**File**: `src/ate_cloud/api/v1/reports.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/reports/atml/{exec_id}` | Export execution as IEEE 1636.1 ATML XML |
| GET | `/api/v1/reports/{format}/{exec_id}` | Export execution report in specified format (`atml`, `csv`, or `parquet`) |

The `{format}` parameter accepts: `atml` (XML), `csv` (flat measurements), `parquet` (columnar binary; falls back to CSV if pyarrow is unavailable).

---

## Unwired Routers (13 routers, 52 endpoints)

These routers exist in the codebase but are NOT registered in `router.py`. They are not active on the running server. See the [Wiring Guide](#wiring-guide) for instructions on activating them.

---

### Auth (prefix: `/auth`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/auth.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/login` | Authenticate with username/password, return JWT tokens |
| POST | `/api/v1/auth/refresh` | Exchange refresh token for new token pair (rotation) |
| GET | `/api/v1/auth/me` | Return current authenticated user info |

Implements refresh token rotation. Requires `JWT_SECRET` to be configured.

---

### Calibrations (prefix: `/calibrations`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/calibrations.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/calibrations` | Record a calibration result (create or update) |
| GET | `/api/v1/calibrations` | List calibration records (optional instrument_id, status filters) |
| GET | `/api/v1/calibrations/status` | Check calibration status for an instrument (query param) |
| POST | `/api/v1/calibrations/check-expiry` | Refresh status for all calibration records |
| GET | `/api/v1/calibrations/{instrument_id}` | Get latest calibration record for an instrument |
| PUT | `/api/v1/calibrations/{instrument_id}` | Update a calibration record |
| DELETE | `/api/v1/calibrations/{instrument_id}` | Delete calibration records for an instrument |

The `/status` and `/check-expiry` endpoints are registered before `/{instrument_id}` to avoid path parameter conflicts.

---

### Debug (prefix: `/debug`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/debug.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/debug/breakpoints` | Create a new debug breakpoint |
| GET | `/api/v1/debug/breakpoints` | List breakpoints (optional session_id filter) |
| GET | `/api/v1/debug/breakpoints/{bp_id}` | Get a breakpoint by ID |
| PUT | `/api/v1/debug/breakpoints/{bp_id}` | Update an existing breakpoint |
| DELETE | `/api/v1/debug/breakpoints/{bp_id}` | Delete a breakpoint |

Writing endpoints (POST, PUT, DELETE) require `ATE_DEV_MODE=true`. Reading endpoints (GET) work in both modes.

---

### Diagnose (prefix: `/diagnose`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/diagnose.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/diagnose` | Diagnose a test failure using hybrid RAG + Neo4j FMEA + LLM |
| POST | `/api/v1/diagnose/{diagnosis_id}/feedback` | Submit operator feedback on a diagnosis (confirm/reject) |

Requires Qdrant (vector DB), Neo4j (graph DB), and OpenAI API key. The diagnosis pipeline: hybrid retrieval (Qdrant semantic + Neo4j causal) -> RRF fusion -> LLM analysis -> structured diagnosis with root cause, confidence, evidence citations, and repair steps.

---

### Faults (prefix: `/faults`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/faults.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/faults/seed` | Seed the Neo4j FMEA knowledge graph with 100+ electronics fault records |
| POST | `/api/v1/faults/evolve` | Evolve the knowledge graph from new diagnosis feedback |

`POST /seed` is idempotent (uses MERGE). `POST /evolve` performs synonym detection via cosine similarity (threshold 0.85), creates new entities if novel, and degrades stale edges.

---

### Limits (NO prefix — needs one before wiring)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/limits.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/limits` | List all test limits (optional product_type filter) |
| GET | `/api/v1/limits/resolve` | Resolve the effective limit for a product and measurement at a given date |
| GET | `/api/v1/limits/{limit_id}` | Get a test limit by business identifier |
| POST | `/api/v1/limits` | Create a new test limit version |
| PUT | `/api/v1/limits/{limit_id}` | Update an existing test limit |
| DELETE | `/api/v1/limits/{limit_id}` | Delete a test limit |

The router is created with `APIRouter()` (no prefix). A prefix must be added before wiring. The `/resolve` endpoint is registered before `/{limit_id}` to avoid path parameter conflicts.

---

### Operator Checkpoints (prefix: `/executions`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/operator_checkpoints.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/executions/{run_id}/checkpoint/pending` | Get the currently pending operator checkpoint for a run |
| POST | `/api/v1/executions/{run_id}/checkpoint` | Submit operator response to a pending checkpoint |

Shares the `/executions` prefix with the executions router. Pending checkpoints are tracked by `CheckpointHandler` instances on `app.state.checkpoint_handlers`.

---

### Products (NO prefix — needs one before wiring)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/products.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/products` | List all product configs with pagination |
| GET | `/api/v1/products/{product_type}` | Get a product config by product type |
| POST | `/api/v1/products` | Create a new product config |
| PUT | `/api/v1/products/{product_type}` | Update a product config by product type |
| DELETE | `/api/v1/products/{product_type}` | Delete a product config by product type |

The router is created with `APIRouter()` (no prefix). A prefix must be added before wiring. Product configs are reference data defining testing templates (sequences, limits, instruments, checkpoints).

---

### Recordings (prefix: `/executions`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/recordings.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/executions/{run_id}/record` | Start recording execution events to JetStream |
| GET | `/api/v1/executions/{run_id}/recording` | Get recording status for a session |
| GET | `/api/v1/executions/{run_id}/recordings` | List recorded events (read-only, no replay delays) |
| POST | `/api/v1/executions/{run_id}/replay` | Start replaying recorded events (synchronous, returns all events) |
| POST | `/api/v1/executions/{run_id}/replay/diff` | Compute diff between original and replayed event sequences |
| POST | `/api/v1/executions/{run_id}/replay/pause` | Pause an active streaming replay |
| POST | `/api/v1/executions/{run_id}/replay/resume` | Resume a paused streaming replay |
| GET | `/api/v1/executions/{run_id}/replay/stream` | SSE stream of replayed events with time-accurate delays |

Requires NATS JetStream. The recorder/replay uses the `ATE_EXECUTION_EVENTS` stream with subject `ate.execution.{run_id}.events`. All endpoints return `503` if NATS is unavailable (no silent degradation).

---

### SPC (prefix: `/spc`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/spc.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/spc/{product}/{measurement}` | Get SPC statistics (Cp, Cpk, Ppk, mean, sigma) for a measurement stream |
| GET | `/api/v1/spc/{product}/{measurement}/chart` | Get X-bar/R control chart data (center line, control limits, subgroups) |
| GET | `/api/v1/spc/alerts` | Get recent SPC alerts from the streaming processor |

Statistics and chart endpoints load recent measurements from the database and compute per-request (no shared state). The alerts endpoint returns data from an in-memory streaming processor attached to `app.state.spc_processor`; empty list if none is running.

---

### Trace (prefix: `/trace`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/trace.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/trace/{serial}` | Get traceability data by serial number (W3C PROV JSON-LD) |
| GET | `/api/v1/trace/{serial}/structured` | Get structured traceability data (chronological steps with instruments and measurements) |

The JSON-LD response includes `@context` and `@graph` keys for PROV-aware consumers. Returns `404` if no trace data exists for the serial number.

---

### Workers (prefix: `/workers`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/workers.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workers` | List all registered edge workers from the `ate-workers` KV bucket |
| GET | `/api/v1/workers/{worker_id}` | Get a single worker's metadata |
| GET | `/api/v1/workers/{worker_id}/health` | Get worker online/offline health status |
| GET | `/api/v1/workers/{worker_id}/history` | Get worker heartbeat time-series from the database |

Requires NATS JetStream KV bucket. The health endpoint returns `online` if the KV key exists (heartbeated within 30s TTL), `offline` otherwise. Returns `503` if NATS is unavailable.

---

### Workflows (prefix: `/workflows`)

**Status**: ⚠️ Not wired
**File**: `src/ate_cloud/api/v1/workflows.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/workflows` | Create a multi-station workflow definition |
| GET | `/api/v1/workflows/{workflow_id}` | Get a workflow definition by ID |
| GET | `/api/v1/workflows/{workflow_id}/stations/{station_id}/status` | Get station handoff status within a workflow session |

Requires NATS JetStream KV (`ate-handoffs` bucket). Workflows and handoffs are stored in KV. The station status returns one of `pending`, `done`, or `failed`. Returns `502` if NATS/KV is unavailable.

---

## SSE (Server-Sent Events) Endpoints

The platform provides two SSE endpoints for real-time event streaming.

### Execution Event Stream

```
GET /api/v1/executions/{run_id}/events
```

Real-time stream of execution events (step start/complete, measurement, error, etc.). Supports `Last-Event-ID` header for resumption. Sends keep-alive comments every 15 seconds to prevent connection timeout.

**Event format**:
```
event: <event_type>
data: {"run_id": "...", "status": "...", ...}
id: <event_id>
```

Event types include: `EXECUTION_STARTED`, `EXECUTION_COMPLETED`, `STEP_STARTED`, `STEP_COMPLETED`, `MEASUREMENT_RECORDED`, `ERROR_OCCURRED`.

### Replay Event Stream

```
GET /api/v1/executions/{run_id}/replay/stream?speed=1.0
```

Streams recorded events from JetStream with time-accurate delays scaled by the `speed` query parameter (default 1.0). Supports pause/resume via control endpoints. Each SSE event carries the recorded event type on the `event:` line and the full payload as JSON `data:`.

---

## Simulation API

The simulation endpoint is part of the executions router (not a separate router). It starts a simulated execution without requiring physical hardware.

```
POST /api/v1/executions/{run_id}/simulate
```

**Request body**:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tier` | string | `"full"` | Simulation tier: `"driver"`, `"dry_run"`, or `"full"` |
| `noise_model` | string | `"gaussian"` | Noise model: `"gaussian"`, `"drift"`, `"bias"`, `"full"`, `"none"` |
| `noise_sigma` | float | `0.01` | Standard deviation of Gaussian noise |
| `drift_rate` | float | `0.001` | Linear time-dependent drift rate |
| `bias` | float | `0.0` | Constant systematic calibration offset |
| `seed` | integer | `42` | Random seed for reproducible simulations |

**Simulation tiers**:

- **driver** (`"driver"`): Instrument-level simulation. Wraps mock drivers with physics-aware noise injection. No PyVISA or real instruments required.
- **dry_run** (`"dry_run"`): Scheduler-level simulation. Traverses the scheduling graph without executing scripts. Evaluates preconditions, acquires/releases resources, returns per-step decisions (PASS, FAIL, SKIP, BLOCKED, ERROR, NOT_REACHED).
- **full** (`"full"`): Full-chain simulation. Combines driver-level noise injection with scheduler-level traversal. Highest fidelity simulation tier.

---

## Wiring Guide

To activate an unwired router, add it to `src/ate_cloud/api/v1/router.py`:

```python
# src/ate_cloud/api/v1/router.py
from fastapi import APIRouter

# Existing imports (10 wired routers)...
from ate_cloud.api.v1.auth import router as auth_router  # Add this

api_router = APIRouter()

# Existing includes (10 wired routers)...
api_router.include_router(auth_router)  # Add this
```

### Special cases

**Routers that need a prefix** (`limits.py`, `products.py`):

These routers are created with `APIRouter()` (no prefix). They need one before wiring:

```python
# Add prefix in the router file itself
router = APIRouter(prefix="/limits")  # currently: router = APIRouter()
```

Or pass the prefix when including:

```python
from ate_cloud.api.v1.limits import router as limits_router
api_router.include_router(limits_router, prefix="/limits")
```

**Routers that share a prefix** (`executions.py`, `operator_checkpoints.py`, `recordings.py`):

All three use the `/executions` prefix. They can be merged or mounted carefully:

```python
# Option A: Mount each separately (same prefix, different tags)
api_router.include_router(operator_checkpoints_router)  # already has prefix=/executions
api_router.include_router(recordings_router)             # already has prefix=/executions

# Option B: Merge into a single router file (recommended for production)
```

---

## API Authentication

Authentication is provided by the `auth.py` router (currently unwired). When wired, all protected endpoints require JWT authentication.

### Login Flow

```
POST /api/v1/auth/login
Body: {"username": "...", "password": "..."}
Response: {"access_token": "...", "refresh_token": "...", "expires_in": 1800}
```

### Authentication Header

```
Authorization: Bearer <access_token>
```

### Token Details

| Setting | Value |
|---------|-------|
| Access token expiry | `JWT_EXPIRE_MINUTES` (default 30 minutes) |
| Algorithm | RS256 (configurable via `JWT_ALGORITHM`) |
| Refresh token | Rotation-based (old token is revoked on refresh) |
| Scopes | Derived from user role (RBAC) |

### Auth-Protected Endpoints

When the auth router is wired, the `GET /api/v1/auth/me` endpoint returns the current user's info. Other routers can be protected by adding the `get_current_user` dependency to their routes.

---

## Endpoint Summary

| Count | Category |
|-------|----------|
| 47 | Wired endpoints (10 routers) |
| 52 | Unwired endpoints (13 routers) |
| 99 | Total REST endpoints |
| 2 | SSE streaming endpoints |
| 101 | Grand total |

All endpoints are documented above. Request/response body schemas are available in the interactive Swagger docs at `http://localhost:8000/docs` when the server is running.