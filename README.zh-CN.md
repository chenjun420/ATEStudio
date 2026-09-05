# ATE Studio

**面向电子产品产线的自动化测试工程平台。**

[English](README.md) | 简体中文

![Python 3.12](https://img.shields.io/badge/python-3.12-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi)
![Vue 3](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vue.js)
![TypeScript](https://img.shields.io/badge/TypeScript-6.0-3178C6?logo=typescript)
![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![CI](https://github.com/chenjun420/ATEStudio/actions/workflows/ci.yml/badge.svg)

ATE Studio 是面向通信、服务器及消费电子制造的端到端产测工程平台，将事件驱动测试调度、YAML 测试计划 DSL、脚本执行、三级无硬件仿真与可视化流程编排整合于一体。

- **事件驱动调度器** —— 基于状态机的扫描式调度，支持前置条件求值、资源管理、自适应跳过与配置热加载。
- **YAML DSL + 脚本** —— 声明式测试计划（v3.0/v3.2），支持循环、分支、屏障、治具控制与多 UUT 派发；可内联 Python 脚本。
- **三级仿真** —— 仪器级噪声注入、调度器 Dry-Run、全链路仿真，无需任何硬件即可验证测试计划。
- **AI 辅助诊断** —— 混合检索（Qdrant RAG 向量检索 + FalkorDB 本体知识图谱）用于故障定位与诊断。
- **SPC 与追溯** —— 实时控制图、过程能力分析（Cpk/Ppk）、端到端执行追溯与 ATML 报表导出。
- **多工位流程** —— 基于 NATS JetStream KV 的工位交接与上游依赖编排。
- **可视化序列编辑器** —— AntV X6 拖拽式流程编排，内嵌 Monaco Editor 编辑 YAML。

> **目标用户：** 编写和调试测试计划的测试工程师，以及在产线上执行测试的操作员。

---

## 系统架构

ATE Studio 采用云边协同架构。

```
┌─────────────────────────────────────────────────────────────────────┐
│                         云端  (ate_cloud)                           │
│  FastAPI (端口 8000)  ·  SQLAlchemy (SQLite/PostgreSQL/MySQL)       │
│  NATS JetStream  ·  Qdrant 向量库  ·  FalkorDB 图库 (Redis/6379)    │
│                                                                     │
│  REST API + SSE  ·  脚本版本管理  ·  故障索引                       │
│  AI 诊断  ·  SPC 分析  ·  ATML 导出  ·  配置下发                    │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ NATS JetStream
┌──────────────────────────────────┴──────────────────────────────────┐
│                         端侧  (ate_platform)                        │
│  ScannerScheduler (事件驱动)  ·  Executor (上下文代理)              │
│  Drivers (HAL/MAL、gRPC、Mock)  ·  Simulation (驱动/dry-run/全链路)  │
│  Recorder (录制/回放)  ·  Debug (步骤/边断点)                       │
│  DSL 解析器  ·  OpenHTF 适配器 (可选)                               │
└─────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────────────────────────┴──────────────────────────────────┐
│                       前端  (Vue 3 + Vite)                          │
│  序列编辑器 · 仪表盘 · 执行历史 · 工位管理                          │
│  测量数据浏览器 · 操作员视图 · 设置 · 产品换型                      │
│                                                                     │
│  AntV X6 3.x · Element Plus · Monaco · Pinia · Vue Router ·        │
│  Tailwind CSS 4 · vue-i18n                                          │
└─────────────────────────────────────────────────────────────────────┘
```

### 模块结构

```
src/
├── ate_cloud/          # 云侧服务：FastAPI 应用、REST/SSE API、业务服务
│   ├── api/v1/         # API 路由（按域划分模块）
│   ├── auth/           # JWT 认证 + RBAC + SSE 票据
│   ├── db/  models/  schemas/  services/  nats/  observability/
├── ate_platform/       # 端侧执行引擎
│   ├── scheduler/      # 事件驱动扫描调度器、JetStream worker
│   ├── executor/       # 脚本/步骤执行、上下文代理、v3.2 派发器
│   ├── drivers/        # HAL/MAL 驱动、gRPC、MockDriverFactory
│   ├── simulation/     # 三级仿真（仪器 / dry-run / 全链路）
│   ├── recorder/       # 录制与回放
│   ├── scheduler/      # 扫描调度器 + 步骤/边断点暂停门
│   ├── dsl/            # YAML DSL 解析器
│   └── openhtf/        # OpenHTF 适配器（可选 extra）
├── shared/             # 共享类型：DSL、事件、测量、多工位
└── (frontend/)         # Vue 3 单页应用，见 frontend/ 目录
frontend/src/
├── views/  api/  composables/  stores/  router/
tests/                  # 后端测试：unit / integration / e2e
examples/               # 示例测试计划与运行器（mock 模式）
docs/                   # 设计文档（中文）
```

---

## 技术栈

| 领域 | 技术 |
|------|------|
| 运行时 / 包管理 | Python 3.12+、[uv](https://docs.astral.sh/uv/) |
| Web 框架 / ORM | FastAPI、SQLAlchemy 2.0 (async)、Alembic、Pydantic v2 |
| 消息 / 数据 | NATS JetStream、Qdrant（向量）、FalkorDB（图库，Redis/6379） |
| 质量工具 | ruff、mypy (strict)、pytest + pytest-asyncio |
| 前端 | Vue 3.5、TypeScript、Vite、Pinia、Vue Router、vue-i18n |
| 编辑器 / UI | AntV X6、Element Plus、Monaco Editor、Tailwind CSS 4 |
| 前端测试 | Vitest、Vue Test Utils |
| 容器 / CI | Docker Compose、GitHub Actions |

可选 extras：`openhtf`（OpenHTF 集成）。断点为调度器内的步骤级/边级暂停门（无 IDE 附加调试器）。

---

## 快速开始

### 环境要求

- Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)
- Node.js 18+（前端）
- Docker 与 Docker Compose（容器化部署）

### Docker 方式（推荐）

```bash
git clone https://github.com/chenjun420/ATEStudio.git
cd ATEStudio

cp env.template .env
docker compose --profile dev up -d

# 执行数据库迁移
docker exec -it ate-studio-ate-cloud-1 alembic upgrade head
```

随后访问：

- API 文档（Swagger UI）：http://localhost:8000/docs
- FalkorDB 浏览器：http://localhost:3000
- NATS 监控：http://localhost:8222

### 非 Docker 方式

```bash
# 后端
uv sync
uv run alembic upgrade head
uv run uvicorn ate_cloud.main:app --reload --port 8000

# 前端（另开终端）
cd frontend
npm install
npm run dev          # http://localhost:5173，/api 代理到 :8000
```

---

## 仿真模式

三级仿真体系让你在没有物理硬件的情况下开发和验证测试计划。

| 级别 | 组件 | 说明 |
|------|------|------|
| **1 — 驱动级** | `InstrumentSimulator` | 包装 mock 驱动并注入物理感知噪声（高斯抖动、漂移、偏置、全量、或无噪声）。 |
| **2 — Dry-Run** | `DryRunScheduler` | 遍历完整调度图但不执行脚本；求值前置条件、资源、循环与 `skip_if`；逐步骤返回 PASS/FAIL/SKIP/BLOCKED/ERROR/NOT_REACHED。 |
| **3 — 全链路** | `FullChainSimulator` | 组合 1、2 两级：图遍历 + 噪声注入测量。 |

`MockDriverFactory` 提供开箱即用的 mock DMM 与 PSU 驱动，以合理默认值响应 SCPI 查询——无需 PyVISA 或真实仪器。

启用方式：

- **环境变量：** `ATE_SIMULATION_MODE=true`（Docker dev profile 默认开启）
- **API：** `POST /api/v1/executions/{runId}/simulate`，请求体 `{"tier": "driver"|"dry_run"|"full", "noise_model": "GAUSSIAN", "noise_sigma": 0.01, "seed": 42}`
- **CLI：** `python -m examples.run_test voltage_test`（默认使用 mock 驱动）
- **前端：** `useSimulation` composable

---

## API

云侧在 `/api/v1` 下提供版本化 REST API，并通过 SSE 流推送执行/录制/离线状态的实时更新。交互式文档由 FastAPI 自动生成：**http://localhost:8000/docs**（Swagger）与 `/redoc`。

路由按域划分：健康检查与认证、用户与 RBAC、节点模板、脚本（CRUD + AI 生成/版本管理）、序列、执行（+ SSE）、产品换型、仪表盘、资源、报表/ATML、校准、调试、AI 诊断、FMEA 故障、限值、操作员检查点、产品、录制（+ SSE/回放）、SPC、追溯、worker、多工位流程。SSE 挂载点使用一次性票据认证，因为 `EventSource` 无法发送 `Authorization` 请求头。

---

## 配置

主要环境变量（完整列表见 [`env.template`](env.template)）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ATE_CLOUD_DATABASE_TYPE` | `sqlite` | 数据库后端：`sqlite`、`postgresql` 或 `mysql` |
| `ATE_CLOUD_NATS_URL` | `nats://localhost:4222` | NATS JetStream 地址 |
| `ATE_CLOUD_QDRANT_URL` | `http://localhost:6333` | Qdrant 向量库地址 |
| `ATE_SIMULATION_MODE` | `false` | 使用仿真驱动（无硬件） |
| `ATE_DEV_MODE` | `false` | 启用调试特性 / 放宽校验 |
| `FALKORDB_URL` / `FALKORDB_GRAPH` / `FALKORDB_PASSWORD` | `redis://localhost:6379` / `fmea` / _(空)_ | FalkorDB 图数据库（Redis RESP，端口 6379）；密码留空 = 无认证 |
| `JWT_SECRET` / `JWT_ALGORITHM` | — / `RS256` | JWT 签名密钥与算法 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | — | LLM 凭证（OpenAI、DashScope/通义千问等） |
| `OPENAI_MODEL` / `OPENAI_EMBEDDING_MODEL` | `gpt-4o-mini` / `text-embedding-3-small` | 对话与嵌入模型 |
| `ATE_CLOUD_EMBEDDING_DIMENSIONS` | `1536` | 向量嵌入维度 |

---

## 开发

```bash
# 后端依赖（按需安装可选 extras）
uv sync --group dev
uv sync --extra openhtf                   # 可选：OpenHTF 适配器

# 数据库迁移
uv run alembic upgrade head

# 质量门禁（主干上均为干净状态）
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest

# 前端
cd frontend
npm install
npm run dev
npm run test                              # vitest
npm run generate:types                    # 由 shared/dsl.py 重新生成 DSL TS 类型
```

### 部署 profile

`docker compose` 提供两个 profile：

| Profile | 服务 | 用途 |
|---------|------|------|
| `dev` | nats + qdrant + falkordb + ate-cloud + ate-platform | 全栈开发 |
| `cloud` | nats + qdrant + falkordb + ate-cloud | 仅云侧部署 |

裸金属部署时，在目标主机上通过 systemd/nohup 运行同样的服务（NATS、Qdrant、FalkorDB、`ate_cloud`）；启动 uvicorn 时需设置 `PYTHONPATH=src`。FalkorDB 以 Redis 8 服务加载 `falkordb.so` 模块、监听 6379 端口运行（见 [`docs/部署手册-192.168.5.24调试服务器.md`](docs/部署手册-192.168.5.24调试服务器.md)）。

---

## CI/CD

GitHub Actions（`.github/workflows/`）：

- **`ci.yml`** —— 每次 push/PR 到 `main`/`master` 时运行：
  - `lint`：ruff + mypy (strict)
  - `test`：pytest，覆盖率门槛 80%
  - `frontend`：vue-tsc 类型检查 + vitest
  - `security`：bandit（不阻断）
- **`release.yml`** —— 推送 `v*` tag 时：构建并推送 Docker 镜像（semver + sha 标签）。

---

## 文档

设计与系统方案文档（中文）位于 [`docs/`](docs/) 目录：

- `ATE Studio系统方案与详细设计文档.md`
- `电子产品产测上位机软件平台V3.2_完整系统方案.md`

---

## 贡献

欢迎在 [GitHub](https://github.com/chenjun420/ATEStudio) 上提交 issue 或 pull request。提交前请运行 `ruff`、`mypy` 与测试套件。

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。
