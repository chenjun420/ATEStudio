# ATE Studio — 电子产品产测上位机软件平台

Flexible production test engineering platform for communications, servers, and consumer electronics.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue?logo=python)
![Vue 3](https://img.shields.io/badge/vue-3.5-4FC08D?logo=vue.js)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688?logo=fastapi)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 概述 (Overview)

ATE Studio is a production line test engineering platform that provides end-to-end automation for electronics manufacturing test. It combines event-driven test scheduling, a rich YAML DSL for test plans, full scripting capabilities, and visual orchestration into a single integrated platform.

**Key capabilities:**

- **Event-driven scanner scheduler** — state-machine based scheduling with precondition evaluation, resource management, adaptive skip, and config watching
- **3-tier simulation** — instrument-level, scheduler dry-run, and full-chain simulation with physics-aware noise models, enabling test plan validation without hardware
- **AI-assisted diagnosis** — hybrid retrieval (RAG + Neo4j FMEA knowledge graph) for fault diagnosis, with 100+ seeded fault records and continuous knowledge graph evolution
- **SPC (Statistical Process Control)** — real-time control charts, process capability analysis (Cpk/Ppk), and trend detection
- **Traceability** — end-to-end test execution trace with ATML report export
- **Multi-station workflows** — NATS JetStream KV-based station handoff with upstream dependency orchestration
- **Visual sequence editor** — AntV X6 drag-and-drop test flow editing with Monaco Editor for inline YAML editing

**Target users:** Test engineers designing and debugging test plans; production line operators running tests on the factory floor.

---

## 系统架构 (Architecture)

The platform follows a cloud-edge architecture with three layers:

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CLOUD LAYER (ate_cloud)                        │
│                                                                     │
│  ┌──────────┐  ┌────────────┐  ┌───────┐  ┌───────┐  ┌───────────┐ │
│  │ FastAPI  │  │ SQLite /   │  │Qdrant │  │Neo4j  │  │NATS       │ │
│  │ (port    │  │ PostgreSQL │  │Vector │  │Graph  │  │JetStream  │ │
│  │  8000)   │  │ / MySQL    │  │DB     │  │DB     │  │Messaging  │ │
│  └────┬─────┘  └────────────┘  └───────┘  └───────┘  └─────┬─────┘ │
│       │                                                     │       │
│       └────── REST API (99 endpoints) ────── SSE bridge ────┘       │
│                                                                     │
│  Services: Script versioning / Failure indexing / AI diagnosis /    │
│            SPC analytics / ATML export / Config distribution        │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │ NATS JetStream
┌─────────────────────────────────┴───────────────────────────────────┐
│                      EDGE LAYER (ate_platform)                      │
│                                                                     │
│  ┌────────────────┐  ┌──────────┐  ┌───────────┐  ┌─────────────┐  │
│  │ScannerScheduler│  │Executor  │  │Drivers    │  │Simulation   │  │
│  │(event-driven)  │  │(Context  │  │(HAL/MAL,  │  │(3-tier:     │  │
│  │                │  │ Proxy)   │  │ gRPC,     │  │ driver/     │  │
│  │                │  │          │  │ Mock)     │  │ dry-run/full│  │
│  └────────────────┘  └──────────┘  └───────────┘  └─────────────┘  │
│                                                                     │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐      │
│  │Recorder    │  │Debug             │  │OpenHTF Adapter    │      │
│  │(record/    │  │(breakpoint mgr,  │  │(optional)         │      │
│  │ replay)    │  │ debug executor)  │  │                    │      │
│  └────────────┘  └──────────────────┘  └────────────────────┘      │
└─────────────────────────────────────────────────────────────────────┘
                                  │
┌─────────────────────────────────┴───────────────────────────────────┐
│                      FRONTEND (Vue 3)                               │
│                                                                     │
│  SequenceEditor │ Dashboard │ ExecutionHistory │ StationManagement  │
│  MeasurementExplorer │ OperatorView │ Settings │ ProductChangeover  │
│                                                                     │
│  AntV X6 3.x (graph editor) │ Element Plus 2.x │ Monaco Editor     │
│  Pinia (state) │ Vue Router 5 │ Tailwind CSS 4                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Module Structure

```
src/
├── ate_cloud/              # Cloud service (FastAPI, port 8000)
│   ├── api/v1/             # REST API routers (23 router files, 99 endpoints)
│   ├── auth/               # JWT authentication + RBAC
│   ├── db/                 # SQLAlchemy async engine & sessions
│   ├── models/             # SQLAlchemy ORM models
│   ├── nats/               # NATS messaging & SSE bridge
│   ├── observability/      # OpenTelemetry logging & tracing
│   ├── schemas/            # Pydantic request/response schemas
│   ├── services/           # Business logic (26 service modules)
│   └── storage/            # File storage abstractions
├── ate_platform/           # Edge execution engine
│   ├── scheduler/          # Event-driven scanner scheduler, JetStream worker
│   ├── executor/           # Script execution, ContextProxy, step executors
│   ├── drivers/            # HAL/MAL drivers, gRPC, MockDriverFactory
│   ├── simulation/         # 3-tier simulation (InstrumentSimulator, DryRunScheduler, FullChainSimulator)
│   ├── recorder/           # Record/replay
│   ├── debug/              # Breakpoint manager, debug executor
│   ├── dsl/                # YAML DSL parser
│   └── openhtf/            # OpenHTF adapter (optional)
├── shared/                 # Shared types
│   ├── dsl.py              # YAML DSL v3.0 types
│   ├── events.py           # Event definitions (TEMS A4 aligned)
│   ├── measurement.py      # Measurement types
│   ├── multi_station.py    # Multi-station workflow types
│   └── ...
└── frontend/               # Vue 3 frontend
    └── src/
        ├── views/          # SequenceEditor, Dashboard, StationManagement, etc.
        ├── api/            # API client modules
        ├── composables/    # Vue composables (useSimulation, useGraph, etc.)
        ├── stores/         # Pinia stores
        └── router/         # Vue Router configuration
```

---

## 技术栈 (Technology Stack)

| Component | Technology | Version | License |
|-----------|-----------|---------|---------|
| **Runtime** | Python | 3.12+ | PSF |
| **Web Framework** | FastAPI | 0.110+ | MIT |
| **ORM** | SQLAlchemy | 2.0+ (async) | MIT |
| **Validation** | Pydantic | 2.x | MIT |
| **Migrations** | Alembic | 1.13+ | MIT |
| **Package Manager** | uv (Astral) | latest | Apache-2.0 |
| **Linter** | ruff | 0.4+ | MIT |
| **Type Checker** | mypy | 1.10+ | MIT |
| **Testing** | pytest + pytest-asyncio | 8.0+ | MIT |
| **Messaging** | NATS JetStream | 2.10 | Apache-2.0 |
| **Vector DB** | Qdrant | latest | Apache-2.0 |
| **Graph DB** | Neo4j | 5 (Community) | GPL-3.0 |
| **Frontend Framework** | Vue | 3.5 | MIT |
| **Language** | TypeScript | 6.0 | Apache-2.0 |
| **Build Tool** | Vite | 8.x | MIT |
| **Graph Editor** | AntV X6 | 3.1 | MIT |
| **UI Library** | Element Plus | 2.14 | MIT |
| **State Management** | Pinia | 4.0 | MIT |
| **Code Editor** | Monaco Editor | 0.56 | MIT |
| **CSS Framework** | Tailwind CSS | 4.x | MIT |
| **Test Framework** | Vitest | 4.x | MIT |
| **Container** | Docker / Podman | latest | Apache-2.0 |
| **CI/CD** | GitHub Actions | — | — |

---

## 快速开始 (Quick Start)

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Node.js 18+ and pnpm/npm
- Docker and docker-compose (for containerized setup)

### Local Development (Docker)

```bash
# Clone the repository
git clone <repo-url>
cd ATEStudio

# Copy environment template
cp env.template .env

# Start full stack (dev profile)
docker compose --profile dev up -d

# Run database migrations
docker exec -it ate-studio-ate-cloud-1 alembic upgrade head

# Access services:
# - API docs:       http://localhost:8000/docs
# - Neo4j browser:  http://localhost:7474  (neo4j / atestudio)
# - NATS monitor:   http://localhost:8222
```

### Local Development (without Docker)

```bash
# Backend
uv sync
uv run alembic upgrade head
uv run uvicorn ate_cloud.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev   # http://localhost:5173, proxies /api to localhost:8000
```

---

## 仿真模式 (Simulation Mode)

The platform provides a 3-tier simulation system for developing and validating test plans without physical hardware.

### Tier 1 — Driver Level (驱动级)

`InstrumentSimulator` wraps a mock driver and injects physics-aware noise into instrument readings. Supports multiple noise models:

- **Gaussian** — random measurement jitter (configurable sigma)
- **Drift** — linear time-dependent offset (simulates warmup/temperature effects)
- **Bias** — constant systematic calibration offset
- **Full** — Gaussian + drift + bias (most realistic)
- **None** — pass-through (no noise)

The `MockDriverFactory` creates working mock HAL/MAL drivers that respond to SCPI queries with sensible defaults. No PyVISA or real instruments required.

Available mock drivers:
- **DMM** — voltage (3.3V/5V/12V/24V), current (0.1–2A), resistance (100Ω–10kΩ)
- **PSU** — 3-channel power supply with state tracking

### Tier 2 — Dry Run (DryRun)

`DryRunScheduler` traverses the complete scheduling graph without executing scripts. It:

- Evaluates all step preconditions via `ConditionEvaluator`
- Acquires/releases resources through `ResourceManager`
- Handles `skip_if` expressions and `YamlLoop` structures (FOR/WHILE/FOREACH)
- Returns per-step decisions: PASS, FAIL, SKIP, BLOCKED, ERROR, NOT_REACHED

Useful for test plan validation before deployment, deadlock detection, and CI/CD pre-flight checks.

### Tier 3 — Full Chain (全链路)

`FullChainSimulator` combines Tier 1 and Tier 2: dry-run traversal of the scheduling graph with noise-injected measurements. The highest fidelity simulation tier, verifying both scheduling logic and measurement noise impact in a single pass.

### How to Enable

| Method | Configuration |
|--------|--------------|
| **Environment** | `ATE_SIMULATION_MODE=true` (Docker defaults to `true`) |
| **API** | `POST /api/v1/executions/{runId}/simulate` with body `{"tier": "driver"\|"dry_run"\|"full", "noise_model": "GAUSSIAN", "noise_sigma": 0.01, "seed": 42}` |
| **CLI** | `python -m examples.run_test voltage_test` (uses mock drivers by default) |
| **Frontend** | Vue composable `useSimulation.ts` with UI labels (驱动级, DryRun, 全链路) |

---

## API 概览 (API Overview)

The API provides 99 endpoints across 23 router files. Currently 10 routers (47 endpoints) are wired in `router.py`; the remaining 13 routers exist but are not yet active.

### Wired Routers

| Router | Prefix | Endpoints | Description |
|--------|--------|-----------|-------------|
| health | `/` | 1 | Database health check |
| node_templates | `/node-templates` | 5 | Node template CRUD (graph editor nodes) |
| scripts | `/scripts` | 9 | Script CRUD + content + version management |
| scripts_generate | `/scripts` | 2 | AI script generation and refinement |
| sequences | `/sequences` | 5 | Test sequence CRUD |
| executions | `/executions` | 6 | Start, list, search, abort, SSE event stream |
| changeover | `/changeover` | 5 | Product changeover optimization |
| dashboard | `/dashboard` | 4 | Production dashboard summary |
| resources | `/resources` | 8 | Human/robot resource management |
| reports | `/reports` | 2 | ATML/report export |

### Unwired Routers (existing but not yet active)

| Router | Endpoints | Description |
|--------|-----------|-------------|
| auth | 3 | JWT authentication & registration |
| calibrations | 7 | Calibration management |
| debug | 5 | Debug breakpoints & inspection |
| diagnose | 2 | AI diagnosis query |
| faults | 2 | FMEA knowledge graph seeding & evolution |
| limits | 6 | Test limit management |
| operator_checkpoints | 2 | Operator checkpoints |
| products | 5 | Product configuration |
| recordings | 8 | Test recording CRUD & replay |
| spc | 3 | SPC control charts |
| trace | 2 | Execution trace |
| workers | 4 | Worker registration & heartbeat |
| workflows | 3 | Multi-station workflow management |

See `docs/api.md` for the full API reference.

---

## 部署 (Deployment)

### 云服务器部署（物理部署）

云服务器 `192.168.5.24` 采用**物理部署（Bare Metal）**方式，所有服务直接运行在操作系统上，不使用 Docker/Podman。

| 服务 | 端口 | 启动方式 |
|------|------|----------|
| NATS JetStream | 4222, 8222 | nohup / systemd |
| Qdrant | 6333 | nohup / systemd |
| Neo4j | 7474, 7687 | systemd |
| ate-cloud (FastAPI) | 8000 | nohup / systemd |

```bash
# SSH 连接服务器
ssh rpdzkj@192.168.5.24

# 安装依赖、配置、启动服务（详见部署指南）
# 快速启动：
cd ~/ATEStudio && source ~/.local/bin/env && source .venv/bin/activate
PYTHONPATH=src nohup python -m uvicorn ate_cloud.main:app --host 0.0.0.0 --port 8000 > /tmp/ate-cloud.log 2>&1 &
```

### 本地开发环境（Docker）

本地开发可使用 Docker Compose，提供两个 profile：

| Profile | Services | Use Case |
|---------|----------|----------|
| `dev` | nats + qdrant + neo4j + ate-cloud + ate-platform | Full stack development |
| `cloud` | nats + qdrant + neo4j + ate-cloud | Cloud-only deployment |

```bash
# Full stack (development)
docker compose --profile dev up -d

# Cloud only
docker compose --profile cloud up -d
```

See `docs/deployment.md` for the full deployment guide.

---

## 配置 (Configuration)

Key environment variables (see `env.template` for the complete list):

| Variable | Default | Description |
|----------|---------|-------------|
| `ATE_CLOUD_DATABASE_TYPE` | `sqlite` | Database backend: `sqlite`, `postgresql`, or `mysql` |
| `ATE_CLOUD_NATS_URL` | `nats://localhost:4222` | NATS JetStream connection URL |
| `ATE_CLOUD_QDRANT_URL` | `http://localhost:6333` | Qdrant vector database URL |
| `ATE_SIMULATION_MODE` | `false` | Enable simulation drivers (no real hardware) |
| `ATE_DEV_MODE` | `false` | Enable debug features and relaxed checks |
| `NEO4J_URL` | `bolt://localhost:7687` | Neo4j Bolt connection URL |
| `NEO4J_PASSWORD` | `atestudio` | Neo4j password |
| `JWT_SECRET` | — | JWT signing key (required for auth) |
| `JWT_ALGORITHM` | `RS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINUTES` | `30` | JWT token expiration |
| `OPENAI_API_KEY` | — | LLM API key (OpenAI, Aliyun DashScope, etc.) |
| `OPENAI_BASE_URL` | — | LLM API base URL (empty for OpenAI default; Aliyun DashScope URL for Qwen) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Chat model name (e.g. `gpt-4o-mini`, `qwen-plus`) |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model name (e.g. `text-embedding-3-small`, `qwen3.7-text-embedding`) |
| `ATE_CLOUD_EMBEDDING_DIMENSIONS` | `1536` | Vector embedding dimensions |

---

## 开发 (Development)

```bash
# Install backend dependencies
uv sync --group dev

# Run database migrations
uv run alembic upgrade head

# Run tests
uv run pytest

# Lint and type check
uv run ruff check src/
uv run mypy src/

# Frontend
cd frontend
npm install
npm run dev

# Generate DSL TypeScript types for frontend
cd frontend && npm run generate:types
```

---

## 测试 (Testing)

```bash
# Backend tests with coverage
uv run pytest --cov=src --cov-report=html

# Frontend tests
cd frontend && npm run test

# Example scripts (mock mode, no hardware needed)
python -m examples.run_test voltage_test
python -m examples.run_test current_test
python -m examples.run_test power_on_test
```

---

## 项目结构 (Project Structure)

```
ATEStudio/
├── src/
│   ├── ate_cloud/            # Cloud service (FastAPI + services)
│   ├── ate_platform/         # Edge execution engine
│   └── shared/               # Shared types (DSL, events, measurements)
├── frontend/                 # Vue 3 + TypeScript frontend
│   └── src/
│       ├── views/            # Page components
│       ├── api/              # API client modules
│       ├── composables/      # Vue composables
│       ├── stores/           # Pinia stores
│       └── router/           # Vue Router configuration
├── tests/                    # Backend tests (unit, integration, e2e)
│   ├── cloud/                # ate_cloud tests
│   ├── scheduler/            # Scheduler tests
│   ├── drivers/              # Driver tests
│   └── ...
├── alembic/                  # Database migrations
├── examples/                 # Example test scripts and runner
│   └── scripts/              # voltage_test, current_test, power_on_test
├── config/                   # Configuration files (NATS leaf node, etc.)
├── scripts/                  # Utility scripts (DSL type generation, etc.)
├── .github/workflows/        # CI/CD pipeline definitions
├── docker-compose.yml        # Docker Compose deployment
├── Dockerfile                # Multi-stage Docker build
├── pyproject.toml            # Python project configuration
├── env.template              # Environment variable template
└── 实现方案.md                # Original design document (Chinese)
```

---

## CI/CD

The project uses GitHub Actions for continuous integration and delivery.

**CI pipeline (`.github/workflows/ci.yml`)** — runs on every push/PR to `main`/`master`:

| Job | Tools | Description |
|-----|-------|-------------|
| `lint` | ruff + mypy | Python linting and static type checking |
| `test` | pytest + pytest-cov | Backend tests with ≥80% coverage threshold |
| `frontend` | vue-tsc + vitest | Vue type checking and frontend tests |
| `security` | bandit | Python security scan (non-blocking) |

**Release pipeline (`.github/workflows/release.yml`)** — triggered on version tags (`v*`):

- Builds and pushes Docker images to Docker Hub
- Tags: `semver` (version, major.minor) and `sha`

---

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Contributing

Contributions are welcome. Please open an issue or pull request on the project repository.