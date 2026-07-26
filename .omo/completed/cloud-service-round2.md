# ATE Platform 第二轮 - 云侧服务

## TL;DR

> **快速摘要**: 创建云侧 FastAPI 服务，包括步骤脚本库管理 API、NATS 消息订阅、共享类型模块。暂不配置数据库，聚焦 API 层开发。
> 
> **交付物**:
> - 独立包 `src/ate_cloud/` (FastAPI 服务)
> - 共享类型模块 `src/shared/`
> - 步骤脚本库管理 API (CRUD)
> - NATS JetStream 订阅端
> - API 文档 (OpenAPI/Swagger)
> 
> **预估工作量**: Medium (约 20-30 小时)
> **并行执行**: YES - 3 Waves
> **关键路径**: 共享类型 → FastAPI 核心 → API 端点

---

## Context

### 原始需求
基于实现方案.md，第二轮开发云侧服务层。

### 第一轮已完成
- 事件驱动调度引擎 (EventBus, ScannerScheduler)
- 进程隔离执行器 (ProcessExecutor, ContextProxy)
- PyVISA 仪器驱动框架 (InstrumentDriver, DriverRegistry)
- 数据缓存上传 (SQLiteCache, NATSPublisher, ResumeManager)
- 344 个测试用例

### 技术决策
- **项目结构**: 独立包 `src/ate_cloud/`
- **认证**: 无（暂缓）
- **数据库**: 暂不配置，先开发 API
- **共享类型**: 创建 `src/shared/` 模块

---

## Work Objectives

### Core Objective
创建可独立运行的 FastAPI 服务，提供步骤脚本库管理 API，并订阅端侧 NATS 消息。

### Concrete Deliverables
- `src/shared/` - 共享类型模块
- `src/ate_cloud/` - FastAPI 服务包
- `src/ate_cloud/api/` - API 路由
- `src/ate_cloud/nats/` - NATS 订阅端
- `tests/cloud/` - 云服务测试

### Definition of Done
- [ ] `uv run uvicorn ate_cloud.main:app` 启动成功
- [ ] `GET /api/v1/scripts` 返回空列表
- [ ] NATS 订阅端接收消息并打印

### Must Have
- FastAPI 应用工厂模式
- 步骤脚本库 CRUD API (POST, GET, GET/:id, PUT/:id, DELETE/:id)
- NATS JetStream 订阅 (subject: `ate.>`)
- Pydantic 模型验证
- OpenAPI 文档

### Must NOT Have (Guardrails)
- 不引入 PostgreSQL/SQLAlchemy（暂缓）
- 不引入 Qdrant（暂缓）
- 不实现认证
- 不实现分布式锁
- 不添加前端代码
- 不添加缓存层 (Redis 等)
- 不添加 rate limiting
- 不添加 WebSocket 支持
- 不添加后台任务队列
- 不修改共享类型模块（创建后冻结）

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: YES (pytest)
- **Automated tests**: YES (Tests-after)
- **Framework**: pytest + pytest-asyncio + httpx

### QA Policy
每项任务包含 Agent 执行的 QA 场景：
- 使用 httpx 测试 FastAPI 端点
- 使用 mock 测试 NATS 订阅逻辑

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately - 基础设施):
├── Task 1: 共享类型模块 [quick]
├── Task 2: ate_cloud 包结构 + FastAPI 应用工厂 [quick]
├── Task 3: 配置管理 (pydantic-settings) [quick]
└── Task 4: API 依赖注入 (lifespan) [quick]

Wave 2 (After Wave 1 - 核心功能):
├── Task 5: Pydantic 模型 (Script, ScriptCreate, ScriptUpdate) [quick]
├── Task 6: 内存存储 (临时替代数据库) [quick]
├── Task 7: Scripts API - 列表和详情 [quick]
├── Task 8: Scripts API - 创建和更新 [quick]
└── Task 9: Scripts API - 删除 [quick]

Wave 3 (After Wave 2 - NATS 集成):
├── Task 10: NATS 订阅端 [quick]
├── Task 11: 消息处理器 [quick]
└── Task 12: 集成测试 [unspecified-high]

Wave FINAL (验证):
├── Task F1: API 文档验证 [oracle]
├── Task F2: 代码质量审查 [unspecified-high]
├── Task F3: 端到端测试 [unspecified-high]
└── Task F4: 范围一致性检查 [deep]
```

### Dependency Matrix

| Task | Blocked By | Blocks |
|------|------------|--------|
| 1 | - | 5, 10 |
| 2 | - | 3, 4, 5-9 |
| 3 | 2 | 4 |
| 4 | 2, 3 | 5-9 |
| 5 | 1 | 6-9 |
| 6 | 5 | 7-9 |
| 7-9 | 4, 5, 6 | 12 |
| 10 | 1, 2 | 11 |
| 11 | 10 | 12 |
| 12 | 1-11 | F1-F4 |

---

## TODOs

### Wave 1: 基础设施

- [x] 1. 共享类型模块

  **做什么**:
  - 创建 `src/shared/__init__.py`
  - 从 `ate_platform/types.py` 提取 `StepStatus`, `StepResult`
  - 从 `ate_platform/scheduler/event_bus.py` 提取 `EventType`, `Event`
  - 从 `ate_platform/dsl/parser.py` 提取 `YamlStep`, `YamlPlan`
  - 更新原有导入路径

  **不能做**:
  - 不修改类型定义本身
  - 不添加新类型

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 1

  **引用**:
  - `src/ate_platform/types.py` - 现有类型定义
  - `src/ate_platform/scheduler/event_bus.py` - 事件类型

  **验收标准**:
  - [ ] `from shared import StepStatus, StepResult` 可用
  - [ ] 现有测试仍通过

- [x] 2. ate_cloud 包结构 + FastAPI 应用工厂

  **做什么**:
  - 创建 `src/ate_cloud/__init__.py`
  - 创建 `src/ate_cloud/main.py` - FastAPI 应用工厂
  - 创建 `src/ate_cloud/api/__init__.py`
  - 创建 `src/ate_cloud/api/v1/__init__.py`
  - 创建 `src/ate_cloud/api/v1/router.py` - 路由聚合

  **不能做**:
  - 不添加业务逻辑
  - 不添加数据库连接

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 1

  **验收标准**:
  - [ ] `uv run uvicorn ate_cloud.main:app` 启动成功
  - [ ] `GET /docs` 返回 OpenAPI 文档

- [x] 3. 配置管理 (pydantic-settings)

  **做什么**:
  - 添加 `pydantic-settings>=2.0.0` 到依赖
  - 创建 `src/ate_cloud/config.py`
  - 定义 `Settings` 类: `nats_url`, `app_name`, `debug`

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 1 (依赖 Task 2)

  **验收标准**:
  - [ ] `Settings()` 可实例化
  - [ ] 环境变量覆盖生效

- [x] 4. API 依赖注入 (lifespan)

  **做什么**:
  - 在 `main.py` 添加 `lifespan` 上下文管理器
  - 管理 NATS 连接生命周期
  - 提供 `get_nats` 依赖注入

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 1 (依赖 Task 2, 3)

  **验收标准**:
  - [ ] 启动时 NATS 连接成功（可选，不应阻塞启动）
  - [ ] shutdown 时连接关闭

### Wave 2: 核心功能

- [x] 5. Pydantic 模型 (Script)

  **做什么**:
  - 创建 `src/ate_cloud/schemas/__init__.py`
  - 创建 `src/ate_cloud/schemas/script.py`
  - 定义 `ScriptBase`, `ScriptCreate`, `ScriptUpdate`, `ScriptResponse`
  - 字段: id, name, description, script_path, params_schema, tags, created_at, updated_at

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 2 (依赖 Task 1)

  **验收标准**:
  - [ ] Pydantic 验证生效
  - [ ] 类型检查通过

- [x] 6. 内存存储

  **做什么**:
  - 创建 `src/ate_cloud/storage/__init__.py`
  - 创建 `src/ate_cloud/storage/memory.py`
  - 实现 `MemoryStorage` 类: `list`, `get`, `create`, `update`, `delete`
  - 使用 `dict` 作为临时存储

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 2 (依赖 Task 5)

  **验收标准**:
  - [ ] CRUD 操作正常
  - [ ] 线程安全（使用 asyncio.Lock）

- [x] 7. Scripts API - 列表和详情

  **做什么**:
  - 创建 `src/ate_cloud/api/v1/scripts.py`
  - 实现 `GET /api/v1/scripts` - 列表（返回 `{"items": [], "total": 0}`）
  - 实现 `GET /api/v1/scripts/{id}` - 详情（404 如果不存在）

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 2

  **验收标准**:
  - [ ] GIVEN: 无数据, WHEN: `curl GET /api/v1/scripts`, THEN: 返回 `{"items": [], "total": 0}` 和 200
  - [ ] GIVEN: 无该ID, WHEN: `curl GET /api/v1/scripts/nonexistent`, THEN: 返回 404

- [x] 8. Scripts API - 创建和更新

  **做什么**:
  - 实现 `POST /api/v1/scripts` - 创建 (返回 201)
  - 实现 `PUT /api/v1/scripts/{id}` - 更新 (返回 200)

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 2

  **验收标准**:
  - [ ] GIVEN: 有效数据, WHEN: `curl POST /api/v1/scripts -d '{"name":"test.py","script_path":"/scripts/test.py"}'`, THEN: 返回 201 和创建的对象
  - [ ] GIVEN: 已创建, WHEN: `curl PUT /api/v1/scripts/{id} -d '{"name":"updated.py"}'`, THEN: 返回 200 和更新后的对象

- [x] 9. Scripts API - 删除

  **做什么**:
  - 实现 `DELETE /api/v1/scripts/{id}` - 删除

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 2

  **验收标准**:
  - [ ] DELETE 返回 204
  - [ ] 再次 GET 返回 404

### Wave 3: NATS 集成

- [x] 10. NATS 订阅端

  **做什么**:
  - 创建 `src/ate_cloud/nats/__init__.py`
  - 创建 `src/ate_cloud/nats/subscriber.py`
  - 实现 `NATSSubscriber` 类
  - 使用 pull consumer 模式
  - 订阅 `ate.>` subject (复用现有 NATSPublisher 的 stream `ate_results`)
  - 处理启动时 NATS 不可用的情况（日志警告，不崩溃）

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 3 (依赖 Task 1, 2)

  **验收标准**:
  - [ ] `subscriber.start()` 启动后台任务
  - [ ] `subscriber.stop()` 停止订阅
  - [ ] NATS 不可用时不崩溃，仅记录日志

- [x] 11. 消息处理器

  **做什么**:
  - 实现 `_handle_message()` 方法
  - 解析消息体 (JSON)，使用 shared.StepStatus
  - 根据 subject 路由：`ate.results.*` → 结果处理器
  - 打印日志（暂不持久化）
  - 消息处理成功后 ACK，失败后 NAK

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: 可并行，Wave 3 (依赖 Task 10)

  **验收标准**:
  - [ ] 消息解析成功，日志正确
  - [ ] 成功消息 ACK，失败消息 NAK

- [x] 12. 集成测试

  **做什么**:
  - 创建 `tests/cloud/` 目录
  - 创建 `tests/cloud/test_api.py` - API 测试
  - 创建 `tests/cloud/test_nats.py` - NATS 测试 (mock)
  - 使用 httpx AsyncClient 测试

  **推荐 Agent**: `unspecified-high`, Skills: []

  **并行化**: 可并行，Wave 3 (依赖 Task 1-11)

  **验收标准**:
  - [ ] API 测试覆盖 CRUD
  - [ ] NATS 测试覆盖消息处理

---

## Final Verification Wave

- [x] F1. API 文档验证 — `oracle`
  验证 OpenAPI 文档完整，所有端点有描述，Schema 正确。

- [x] F2. 代码质量审查 — `unspecified-high`
  运行 ruff + mypy，检查类型注解，确保无 AI slop。

- [x] F3. 端到端测试 — `unspecified-high`
  启动服务，使用 httpx 执行完整 CRUD 流程，验证 NATS 订阅。

- [x] F4. 范围一致性检查 — `deep`
  验证所有 Must Have 已实现，Must NOT Have 未出现。

---

## Commit Strategy

- **Wave 1**: `feat(cloud): init ate_cloud package with shared types and config`
- **Wave 2**: `feat(cloud): add scripts CRUD API with memory storage`
- **Wave 3**: `feat(cloud): add NATS subscriber for ate events`

---

## Success Criteria

### 验证命令
```bash
# 启动服务
uv run uvicorn ate_cloud.main:app --reload

# 测试
uv run pytest tests/cloud/ -v

# API 文档
curl http://localhost:8000/docs
```

### 最终检查清单
- [ ] FastAPI 服务启动成功
- [ ] API 文档可访问
- [ ] Scripts CRUD 正常
- [ ] NATS 订阅工作
- [ ] 测试通过