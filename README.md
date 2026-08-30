# ATE Studio

**Automated Test Engineering platform for electronics production lines.**

English | [简体中文](README.zh-CN.md)

![Python 3.12](https://img.shields.io/badge/python-3.12-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi)
![Vue 3](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vue.js)
![TypeScript](https://img.shields.io/badge/TypeScript-6.0-3178C6?logo=typescript)
![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![CI](https://github.com/chenjun420/ATEStudio/actions/workflows/ci.yml/badge.svg)

ATE Studio is an end-to-end production test engineering platform for communications, server, and consumer-electronics manufacturing. It combines an event-driven test scheduler, a YAML test-plan DSL, full script execution, three-tier hardware-free simulation, and a visual flow editor in one integrated system.

- **Event-driven scheduler** — state-machine scanner scheduling with precondition evaluation, resource management, adaptive skip, and live config reload.
- **YAML DSL + scripting** — declarative test plans (v3.0/v3.2) with loops, branches, barriers, fixture control, and multi-UUT dispatch; inline Python scripting.
- **Three-tier simulation** — instrument-level noise injection, scheduler dry-run, and full-chain simulation, so plans can be validated without any hardware.
- **AI-assisted diagnosis** — hybrid retrieval (RAG vector search + Neo4j FMEA knowledge graph) for fault localization and diagnosis.
- **SPC & traceability** — real-time control charts, process-capability analysis (Cpk/Ppk), end-to-end execution trace, and ATML report export.
- **Multi-station workflows** — NATS JetStream KV-based station handoff with upstream-dependency orchestration.
- **Visual sequence editor** — AntV X6 drag-and-drop flow editing with Monaco Editor for inline YAML.

> **Target users:** test engineers authoring and debugging test plans, and production-line operators running tests on the factory floor.

---

## Architecture

ATE Studio follows a cloud–edge architecture.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CLOUD  (ate_cloud)                          │
│  FastAPI (port 8000)  ·  SQLAlchemy (SQLite/PostgreSQL/MySQL)       │
│  NATS JetStream  ·  Qdrant vector DB  ·  Neo4j graph DB             │
│                                                                     │
│  REST API + SSE  ·  script versioning  ·  failure indexing         │
│  AI diagnosis  ·  SPC analytics  ·  ATML export  ·  config push     │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ NATS JetStream
┌──────────────────────────────────┴──────────────────────────────────┐
│                         EDGE  (ate_platform)                        │
│  ScannerScheduler (event-driven)  ·  Executor (Context Proxy)       │
│  Drivers (HAL/MAL, gRPC, Mock)  ·  Simulation (driver/dry-run/full) │
│  Recorder (record/replay)  ·  Debug (breakpoints, debugpy)          │
│  DSL parser  ·  OpenHTF adapter (optional)                          │
└─────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────┴──────────────────────────────────┐
│                       FRONTEND  (Vue 3 + Vite)                      │
│  SequenceEditor · Dashboard · ExecutionHistory · StationManagement  │
│  MeasurementExplorer · OperatorView · Settings · ProductChangeover  │
│                                                                     │
│  AntV X6 3.x · Element Plus · Monaco · Pinia · Vue Router ·         │
│  Tailwind CSS 4 · vue-i18n                                          │
└─────────────────────────────────────────────────────────────────────┘
```

### Module layout

```
src/
├── ate_cloud/          # Cloud service: FastAPI app, REST/SSE API, services
│   ├── api/v1/         # API routers (one module per domain)
│   ├── auth/           # JWT authentication + RBAC + SSE tickets
│   ├── db/  models/  schemas/  services/  nats/  observability/
├── ate_platform/       # Edge execution engine
│   ├── scheduler/      # Event-driven scanner scheduler, JetStream worker
│   ├── executor/       # Script/step execution, ContextProxy, v3.2 dispatcher
│   ├── drivers/        # HAL/MAL drivers, gRPC, MockDriverFactory
│   ├── simulation/     # 3-tier simulation (instrument / dry-run / full-chain)
│   ├── recorder/       # Record & replay
│   ├── debug/          # Breakpoint manager, debugpy executor
│   ├── dsl/            # YAML DSL parser
│   └── openhtf/        # OpenHTF adapter (optional extra)
├── shared/             # Shared types: DSL, events, measurements, multi-station
└── (frontend/)         # Vue 3 SPA — see frontend/ directory
frontend/src/
├── views/  api/  composables/  stores/  router/
tests/                  # Backend tests: unit / integration / e2e
examples/               # Example test plans and runner (mock mode)
docs/                   # Design documents (Chinese)
```

---

## Tech stack

| Area | Technology |
|------|------------|
| Runtime / package manager | Python 3.12+, [uv](https://docs.astral.sh/uv/) |
| Web framework / ORM | FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Messaging / data | NATS JetStream, Qdrant, Neo4j 5 |
| Quality tools | ruff, mypy (strict), pytest + pytest-asyncio |
| Frontend | Vue 3.5, TypeScript, Vite, Pinia, Vue Router, vue-i18n |
| Editor / UI | AntV X6, Element Plus, Monaco Editor, Tailwind CSS 4 |
| Frontend tests | Vitest, Vue Test Utils |
| Containers / CI | Docker Compose, GitHub Actions |

Optional extras: `openhtf` (OpenHTF integration), `debug` (debugpy for IDE-attached edge debugging).

---

## Quick start

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 18+ (for the frontend)
- Docker & Docker Compose (for the containerized stack)

### With Docker (recommended)

```bash
git clone https://github.com/chenjun420/ATEStudio.git
cd ATEStudio

cp env.template .env
docker compose --profile dev up -d

# Run database migrations
docker exec -it ate-studio-ate-cloud-1 alembic upgrade head
```

Then open:

- API docs (Swagger UI): http://localhost:8000/docs
- Neo4j browser: http://localhost:7474
- NATS monitor: http://localhost:8222

### Without Docker

```bash
# Backend
uv sync
uv run alembic upgrade head
uv run uvicorn ate_cloud.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to :8000
```

---

## Simulation

Three tiers let you develop and validate test plans without physical hardware.

| Tier | Component | What it does |
|------|-----------|--------------|
| **1 — Driver** | `InstrumentSimulator` | Wraps a mock driver and injects physics-aware noise (Gaussian jitter, drift, bias, full, or none). |
| **2 — Dry run** | `DryRunScheduler` | Traverses the full scheduling graph without running scripts; evaluates preconditions, resources, loops, and `skip_if`; returns per-step PASS/FAIL/SKIP/BLOCKED/ERROR/NOT_REACHED. |
| **3 — Full chain** | `FullChainSimulator` | Combines tiers 1 and 2: graph traversal with noise-injected measurements. |

The `MockDriverFactory` provides working mock DMM and PSU drivers that answer SCPI queries with sensible defaults — no PyVISA or real instruments required.

Enable it via:

- **Environment:** `ATE_SIMULATION_MODE=true` (the Docker dev profile defaults to this)
- **API:** `POST /api/v1/executions/{runId}/simulate` with `{"tier": "driver"|"dry_run"|"full", "noise_model": "GAUSSIAN", "noise_sigma": 0.01, "seed": 42}`
- **CLI:** `python -m examples.run_test voltage_test` (mock drivers by default)
- **Frontend:** the `useSimulation` composable

---

## API

The cloud service exposes a versioned REST API under `/api/v1` plus Server-Sent-Events streams for live execution/recording/offline updates. Interactive documentation is generated by FastAPI at **http://localhost:8000/docs** (Swagger) and `/redoc`.

Routers are organized by domain: health & auth, users & RBAC, node templates, scripts (CRUD + AI generation/versioning), sequences, executions (+ SSE), product changeover, dashboard, resources, reports/ATML, calibrations, debug, AI diagnosis, FMEA faults, limits, operator checkpoints, products, recordings (+ SSE/replay), SPC, trace, workers, and multi-station workflows. SSE mounts use one-time ticket auth because `EventSource` cannot send an `Authorization` header.

---

## Configuration

Key environment variables (see [`env.template`](env.template) for the full list):

| Variable | Default | Description |
|----------|---------|-------------|
| `ATE_CLOUD_DATABASE_TYPE` | `sqlite` | Backend: `sqlite`, `postgresql`, or `mysql` |
| `ATE_CLOUD_NATS_URL` | `nats://localhost:4222` | NATS JetStream URL |
| `ATE_CLOUD_QDRANT_URL` | `http://localhost:6333` | Qdrant vector DB URL |
| `ATE_SIMULATION_MODE` | `false` | Use simulation drivers (no hardware) |
| `ATE_DEV_MODE` | `false` | Enable debug features / relaxed checks |
| `NEO4J_URL` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `atestudio` | Neo4j connection |
| `JWT_SECRET` / `JWT_ALGORITHM` | — / `RS256` | JWT signing key and algorithm |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | — | LLM credentials (OpenAI, DashScope/Qwen, …) |
| `OPENAI_MODEL` / `OPENAI_EMBEDDING_MODEL` | `gpt-4o-mini` / `text-embedding-3-small` | Chat & embedding models |
| `ATE_CLOUD_EMBEDDING_DIMENSIONS` | `1536` | Vector embedding dimensions |

---

## Development

```bash
# Backend dependencies (include the optional extras when needed)
uv sync --group dev
uv sync --extra openhtf --extra debug     # optional: OpenHTF, debugpy

# Database migrations
uv run alembic upgrade head

# Quality gates (both are clean on main)
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest

# Frontend
cd frontend
npm install
npm run dev
npm run test                              # vitest
npm run generate:types                    # regenerate DSL TS types from shared/dsl.py
```

### Deployment profiles

`docker compose` defines two profiles:

| Profile | Services | Use case |
|---------|----------|----------|
| `dev` | nats + qdrant + neo4j + ate-cloud + ate-platform | Full-stack development |
| `cloud` | nats + qdrant + neo4j + ate-cloud | Cloud-only deployment |

For bare-metal deployment, run the same services (NATS, Qdrant, Neo4j, `ate_cloud`) via systemd/nohup on the target host; set `PYTHONPATH=src` when launching uvicorn.

---

## CI/CD

GitHub Actions (`.github/workflows/`):

- **`ci.yml`** — on every push/PR to `main`/`master`:
  - `lint`: ruff + mypy (strict)
  - `test`: pytest with an 80% coverage threshold
  - `frontend`: vue-tsc type check + vitest
  - `security`: bandit (non-blocking)
- **`release.yml`** — on `v*` tags: builds and pushes Docker images (semver + sha tags).

---

## Documentation

Design and system-spec documents (in Chinese) live in [`docs/`](docs/):

- `ATE Studio系统方案与详细设计文档.md`
- `电子产品产测上位机软件平台V3.2_完整系统方案.md`

---

## Contributing

Contributions are welcome — please open an issue or pull request on [GitHub](https://github.com/chenjun420/ATEStudio). Run `ruff`, `mypy`, and the test suite before submitting.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
