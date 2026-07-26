# ATE Platform 第三轮 - 数据库集成

## TL;DR

> **快速摘要**: 集成 PostgreSQL 数据库，实现脚本库持久化、用户管理、序列定义存储。使用 SQLAlchemy 2.x 异步 ORM。
> 
> **交付物**:
> - 数据库模型定义 (Scripts, Sequences, Users)
> - SQLAlchemy 异步会话管理
> - 脚本库 API 改造（从内存存储到数据库）
> - 数据库迁移脚本
> - 集成测试
> 
> **预估工作量**: Medium
> **并行执行**: YES - 3 Waves
> **关键路径**: 数据库配置 → 模型定义 → API 改造

---

## Context

### 原始需求
基于实现方案.md，第三轮开发数据库集成，实现脚本库持久化。

### 前序已完成
- Round 1: 边缘侧调度引擎 (ate_platform)
- Round 2: 云侧服务基础 (ate_cloud + 内存存储)

### 技术决策
- **数据库**: PostgreSQL 16.x
- **ORM**: SQLAlchemy 2.x (async)
- **迁移**: Alembic
- **连接池**: asyncpg

---

## Work Objectives

### Core Objective
将脚本库从内存存储迁移到 PostgreSQL，支持持久化和多实例部署。

### Concrete Deliverables
- `src/ate_cloud/db/` - 数据库模块
- `src/ate_cloud/models/` - ORM 模型
- `alembic/` - 迁移脚本
- 改造后的 Scripts API

### Definition of Done
- [ ] PostgreSQL 连接配置
- [ ] Scripts 表 CRUD
- [ ] Sequences 表（可选，Round 4 完善）
- [ ] API 使用数据库存储
- [ ] 迁移脚本可执行
- [ ] 测试通过

### Must Have
- Scripts 表持久化
- 异步 SQLAlchemy 会话
- 数据库健康检查

### Must NOT Have
- 认证授权（Round 4+）
- 复杂权限控制
- 多租户

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: pytest + pytest-asyncio
- **Automated tests**: TDD for models and API
- **Agent-Executed QA**: curl tests against running API

---

## Execution Strategy

### Wave 1: 数据库基础设施 (4 tasks)
- Task 1: SQLAlchemy 异步配置
- Task 2: 数据库连接池和会话管理
- Task 3: Alembic 迁移初始化
- Task 4: 健康检查端点

### Wave 2: 数据模型 (3 tasks)
- Task 5: Script 模型定义
- Task 6: Sequence 模型定义（基础）
- Task 7: 模型单元测试

### Wave 3: API 改造 (5 tasks)
- Task 8: Scripts API - 使用数据库
- Task 9: 脚本内容文件存储
- Task 10: 错误处理和事务
- Task 11: 集成测试
- Task 12: API 文档更新

### Final Verification Wave (4 tasks)
- F1: 数据库连接验证
- F2: API CRUD 测试
- F3: 迁移脚本验证
- F4: 代码质量审查

---

## TODOs

### Wave 1: 数据库基础设施

- [x] 1. SQLAlchemy 异步配置

  **做什么**:
  - 添加 `sqlalchemy[asyncio]`, `asyncpg`, `alembic` 依赖
  - 创建 `src/ate_cloud/db/__init__.py`
  - 创建 `src/ate_cloud/db/engine.py` - 异步引擎配置
  - 创建 `src/ate_cloud/db/session.py` - async_session_factory

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 1 开始

  **验收标准**:
  - [ ] `async_engine` 可创建
  - [ ] `async_session_factory` 正常工作

- [x] 2. 数据库连接池和会话管理

  **做什么**:
  - 配置连接池参数
  - 实现 `get_db` 依赖注入
  - 在 lifespan 中管理连接

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 1 (依赖 Task 1)

  **验收标准**:
  - [ ] `get_db()` 返回 AsyncSession
  - [ ] 连接池配置生效

- [x] 3. Alembic 迁移初始化

  **做什么**:
  - `alembic init alembic`
  - 配置 `alembic.ini` 和 `env.py`
  - 支持异步迁移

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 1

  **验收标准**:
  - [ ] `alembic revision` 可创建迁移
  - [ ] `alembic upgrade head` 可执行

- [x] 4. 健康检查端点

  **做什么**:
  - 添加 `GET /health/db` 端点
  - 检查数据库连接
  - 返回连接状态

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 1

  **验收标准**:
  - [ ] 数据库正常时返回 200
  - [ ] 数据库异常时返回 503

### Wave 2: 数据模型

- [x] 5. Script 模型定义

  **做什么**:
  - 创建 `src/ate_cloud/models/__init__.py`
  - 创建 `src/ate_cloud/models/script.py`
  - 定义 Script 表：id, name, description, script_path, params_schema, tags, created_at, updated_at

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 2 (依赖 Wave 1)

  **验收标准**:
  - [ ] 模型可导入
  - [ ] 表结构正确

- [x] 6. Sequence 模型定义（基础）

  **做什么**:
  - 创建 `src/ate_cloud/models/sequence.py`
  - 定义 Sequence 表：id, name, description, yaml_content, created_at, updated_at

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 2

  **验收标准**:
  - [ ] 模型可导入

- [x] 7. 模型单元测试

  **做什么**:
  - 创建 `tests/cloud/test_models.py`
  - 测试模型创建、字段验证

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 2

  **验收标准**:
  - [ ] 测试通过

### Wave 3: API 改造

- [x] 8. Scripts API - 使用数据库

  **做什么**:
  - 修改 `src/ate_cloud/api/v1/scripts.py`
  - 使用数据库会话替代内存存储
  - 实现 CRUD 操作

  **推荐 Agent**: `unspecified-high`, Skills: []

  **并行化**: Wave 3 (依赖 Wave 2)

  **验收标准**:
  - [ ] POST 创建数据入库
  - [ ] GET 从数据库读取
  - [ ] PUT 更新数据库
  - [ ] DELETE 从数据库删除

- [x] 9. 脚本内容文件存储

  **做什么**:
  - 实现脚本文件上传/下载
  - 文件存储在本地目录
  - 数据库记录文件路径

  **推荐 Agent**: `unspecified-high`, Skills: []

  **并行化**: Wave 3

  **验收标准**:
  - [ ] POST 上传脚本文件
  - [ ] GET 下载脚本文件

- [x] 10. 错误处理和事务

  **做什么**:
  - 添加数据库错误处理
  - 事务回滚处理
  - 唯一约束冲突处理

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 3

  **验收标准**:
  - [ ] 重复名称返回 409
  - [ ] 事务失败自动回滚

- [x] 11. 集成测试

  **做什么**:
  - 更新 `tests/cloud/test_api.py`
  - 使用测试数据库
  - 测试完整 CRUD 流程

  **推荐 Agent**: `unspecified-high`, Skills: []

  **并行化**: Wave 3

  **验收标准**:
  - [ ] 所有测试通过

- [x] 12. API 文档更新

  **做什么**:
  - 更新 OpenAPI 描述
  - 添加数据库相关状态码

  **推荐 Agent**: `quick`, Skills: []

  **并行化**: Wave 3

  **验收标准**:
  - [ ] 文档完整

### Final Verification Wave

- [x] F1. 数据库连接验证 — `oracle`
  验证 PostgreSQL 连接、连接池、健康检查。

- [x] F2. API CRUD 测试 — `unspecified-high`
  完整 CRUD 流程测试，验证数据持久化。

- [x] F3. 迁移脚本验证 — `quick`
  验证迁移可正向执行、回滚。

- [x] F4. 代码质量审查 — `deep`
  检查异步代码正确性、事务处理、AI slop。

---

## Commit Strategy

- **Wave 1**: `feat(db): add SQLAlchemy async config and Alembic migrations`
- **Wave 2**: `feat(db): add Script and Sequence models`
- **Wave 3**: `feat(api): migrate Scripts API to database storage`

---

## Success Criteria

### 验证命令
```bash
# 数据库迁移
alembic upgrade head

# 启动服务 (需要 PostgreSQL)
uv run uvicorn ate_cloud.main:app --reload

# 测试
uv run pytest tests/cloud/ -v

# 健康检查
curl http://localhost:8000/health/db
```

### 最终检查清单
- [ ] PostgreSQL 连接正常
- [ ] 迁移脚本可执行
- [ ] Scripts API 使用数据库
- [ ] 所有测试通过