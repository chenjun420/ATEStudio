# ATE 产测平台开发计划

## TL;DR

> **快速摘要**: 创建电子产品产测上位机软件平台，采用事件驱动扫描式调度引擎，Python脚本化测试项，PyVISA仪器抽象，SQLite缓存+NATS上传。第一轮开发聚焦端侧四大模块：调度、执行、仪器驱动、数据缓存。
> 
> **交付物**: 
> - 端侧调度引擎（EventBus + ScannerScheduler + ConditionEvaluator）
> - 脚本执行隔离器（ProcessPool + ContextProxy）
> - PyVISA 仪器驱动框架（基类 + 示例驱动）
> - 数据缓存上传模块（SQLite WAL + NATS Publisher）
> - 示例测试脚本和仪器驱动
> 
> **预估工作量**: Large (约 40-60 小时)
> **并行执行**: YES - 4 Waves
> **关键路径**: 基础设施 → 调度核心 → 执行器 → 集成测试

---

## Context

### 原始需求
基于用户提供的方案文档（实现方案.md），创建电子产品产测上位机软件平台。核心创新点是事件驱动扫描式调度引擎，所有测试项通过前置条件声明依赖关系，调度器动态决定执行顺序和并行度。

### 访谈摘要

**技术决策确认**:
- 项目结构: Monorepo（单仓库）
- Python 版本: 3.12+
- 包管理: uv
- 代码质量: Ruff
- 测试框架: pytest（混合 TDD 策略）
- 数据库迁移: Alembic（云侧）
- API 文档: OpenAPI (Swagger)
- 前端状态: Pinia
- 部署策略: 直接部署（虚拟环境）
- 开发优先级: 端侧优先
- 仪器抽象: PyVISA 统一接口

**开发范围**:
- 第一轮: 调度模块 + 端侧执行 + 仪器驱动 + 数据缓存上传
- 云侧: 使用 Mock 或最小化实现
- 前端: 后端 API 优先，X6 编辑器后续开发
- 示例: 包含示例测试脚本和仪器驱动

### 参考项目研究

**OpenHTF 架构借鉴**:
- Phase/Plug 系统: 装饰器驱动的依赖注入
- Measurement 验证: 声明式验证器
- 回调输出: output_callbacks 列表

**TofuPilot 架构借鉴**:
- Scope-aware 资源管理: all/each 生命周期
- YAML 驱动 Procedure
- Worker Pool + gRPC

**MATS 架构借鉴**:
- Template Method: setup → execute → teardown
- Schema 演进: header 变化自动归档

**OpenTAP 架构借鉴**:
- Plugin 系统: 元数据扫描 + 懒加载
- Resource Registry: 单例资源注册表
- Verdict 升级: 只升级不降级

**Fixate 架构借鉴**:
- Pubsub 解耦: UI/Reporting 完全解耦
- Check 异常流: fail-fast 行为
- UI 无关 API: CLI 和 GUI 复用

### Metis 审查

**识别的缺口（已处理）**:

| 缺口类型 | 具体问题 | 处理方式 |
|----------|----------|----------|
| 性能目标缺失 | 无延迟/可靠性 SLA | 设置默认目标: 95% 步骤在 200ms 内启动 |
| 边缘场景缺失 | 死锁检测、资源竞态 | 添加超时熔断和死锁检测任务 |
| 运维问题缺失 | 故障恢复、紧急停止 | 添加异常处理和清理逻辑任务 |
| 假设未验证 | SQLite 并发性能 | 添加性能基准测试任务 |

---

## Work Objectives

### 核心目标
实现端侧四大核心模块：
1. **调度引擎**: 事件驱动扫描式调度器，毫秒级响应
2. **脚本执行**: 进程隔离执行器，超时熔断，ContextProxy
3. **仪器驱动**: PyVISA 抽象层，自动连接，资源锁
4. **数据缓存**: SQLite WAL 缓存，NATS 上传，断点续传

### 具体交付物
- `src/scheduler/` - 调度引擎模块
- `src/executor/` - 脚本执行模块
- `src/drivers/` - 仪器驱动模块
- `src/data/` - 数据缓存上传模块
- `examples/` - 示例脚本和驱动
- `tests/` - 单元测试和集成测试

### 完成定义
- [ ] 调度引擎可以评估前置条件并调度步骤
- [ ] 脚本在隔离进程执行，超时可终止
- [ ] 仪器驱动可以连接、控制、断开设备
- [ ] 测试结果缓存到 SQLite，上传到 NATS
- [ ] 示例测试可以完整运行（Mock 仪器）

### 必须有
- 事件驱动调度核心逻辑
- 进程隔离执行器
- PyVISA 驱动基类
- SQLite 数据缓存
- NATS 消息发布
- pytest 测试覆盖核心模块

### 必须没有（护栏）
- 不实现分布式锁（第一轮）
- 不实现前端可视化
- 不实现 AI 辅助生成
- 不实现云侧 PostgreSQL/Qdrant
- 不添加超出方案文档的 DSL 特性

---

## Verification Strategy

### 测试决策
- **基础设施存在**: YES（新建项目）
- **自动化测试**: YES（混合策略：核心模块 TDD，其他测试后置）
- **框架**: pytest + pytest-asyncio
- **TDD 范围**: 调度核心、执行器隔离逻辑

### QA 策略
每个任务必须包含 Agent 执行的 QA 场景：
- **后端/API**: 使用 Bash（curl）发送请求，验证状态和响应字段
- **库/模块**: 使用 Bash（python REPL）导入、调用、比较输出
- **证据保存**: `.sisyphus/evidence/task-{N}-{scenario-slug}.{ext}`

---

## Execution Strategy

### 并行执行波次

```
Wave 1 (基础设施 - 可并行):
├── Task 1: 项目初始化 + uv 配置 [quick]
├── Task 2: 代码质量配置 (Ruff + mypy) [quick]
├── Task 3: 基础类型定义 [quick]
└── Task 4: 测试框架配置 (pytest) [quick]

Wave 2 (核心模块 - 部分可并行):
├── Task 5: 事件总线 EventBus [quick]
├── Task 6: 变量空间 VariableSpace [quick]
├── Task 7: 条件评估器 ConditionEvaluator [deep]
├── Task 8: 资源管理器 ResourceManager [quick]
└── Task 9: YAML DSL 解析器 [quick]

Wave 3 (执行器和驱动 - 可并行):
├── Task 10: 步骤注册表 StepRegistry [quick]
├── Task 11: 扫描调度器 ScannerScheduler [deep]
├── Task 12: ContextProxy 上下文代理 [quick]
├── Task 13: 进程执行器 ProcessExecutor [deep]
├── Task 14: PyVISA 驱动基类 [quick]
└── Task 15: 示例仪器驱动（DMM, PSU）[quick]

Wave 4 (数据层和集成 - 部分可并行):
├── Task 16: SQLite 缓存管理器 [quick]
├── Task 17: NATS Publisher [quick]
├── Task 18: 断点续传逻辑 [quick]
├── Task 19: 集成测试：完整流程 [unspecified-high]
└── Task 20: 示例测试脚本 [quick]

Wave FINAL (验证):
├── Task F1: 计划合规审计 (oracle)
├── Task F2: 代码质量审查 (unspecified-high)
├── Task F3: 手动 QA 测试 (unspecified-high)
└── Task F4: 范围一致性检查 (deep)
```

### 依赖矩阵

| 任务 | 依赖 | 被依赖 |
|------|------|--------|
| 1-4 | - | 5-20 |
| 5 | 1, 3 | 11 |
| 6 | 1, 3 | 7, 11 |
| 7 | 3, 6 | 11 |
| 8 | 1, 3 | 11, 14 |
| 9 | 1, 3 | 10, 19 |
| 10 | 9 | 11 |
| 11 | 5, 6, 7, 8, 10 | 19 |
| 12 | 1, 3 | 13 |
| 13 | 3, 12 | 19 |
| 14 | 1, 8 | 15 |
| 15 | 14 | 20 |
| 16 | 1, 3 | 18, 19 |
| 17 | 1, 3 | 18, 19 |
| 18 | 16, 17 | 19 |
| 19 | 11, 13, 18 | F3 |
| 20 | 15 | 19 |
| F1-F4 | 1-20 | - |

### Agent 分配摘要

| Wave | 任务数 | 推荐类型 |
|------|--------|----------|
| 1 | 4 | quick × 4 |
| 2 | 5 | quick × 4, deep × 1 |
| 3 | 6 | quick × 4, deep × 2 |
| 4 | 5 | quick × 4, unspecified-high × 1 |
| FINAL | 4 | oracle, unspecified-high × 2, deep |

---

## TODOs

### Wave 1: 基础设施

- [x] 1. 项目初始化 + uv 配置

  **做什么**:
  - 创建项目目录结构（Monorepo 布局）
  - 初始化 uv 项目：`uv init`
  - 配置 `.python-version` (3.12)
  - 创建 `pyproject.toml` 配置依赖分组
  - 创建基础目录：`src/`, `tests/`, `examples/`

  **不能做**:
  - 不添加业务代码
  - 不配置不需要的依赖

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 1
  - **阻塞**: Task 5-20
  - **被阻塞**: 无

  **引用**:
  - uv 官方文档: `https://docs.astral.sh/uv/`
  - Monorepo 结构参考: `D:\chenjun\openhtf\openhtf\` (包组织)

  **验收标准**:
  - [ ] `uv sync` 成功
  - [ ] 目录结构符合 Monorepo 规范
  - [ ] `.python-version` 包含 "3.12"

  **QA 场景**:
  ```
  Scenario: 项目初始化验证
    Tool: Bash
    Steps:
      1. cd F:\Workspace\ATEStudio && uv sync
      2. uv run python --version
    Expected: "Python 3.12.x"
    Evidence: .sisyphus/evidence/task-01-init.txt
  ```

  **提交**: YES
  - Message: `chore: init project structure with uv`
  - Files: `pyproject.toml, .python-version, .gitignore`

- [x] 2. 代码质量配置 (Ruff + mypy)

  **做什么**:
  - 创建 `ruff.toml` 配置文件
  - 配置 linting 规则（E, F, I, N, W, UP）
  - 配置格式化规则（quote-style, line-length=100）
  - 创建 `mypy.ini` 或 `pyproject.toml [tool.mypy]`
  - 配置严格类型检查

  **不能做**:
  - 不添加自定义规则（保持默认）

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 1
  - **阻塞**: 后续代码任务
  - **被阻塞**: Task 1

  **引用**:
  - Ruff 配置: `https://docs.astral.sh/ruff/configuration/`
  - mypy 配置: `https://mypy.readthedocs.io/en/stable/config_file.html`

  **验收标准**:
  - [ ] `uv run ruff check .` 通过
  - [ ] `uv run ruff format --check .` 通过

  **QA 场景**:
  ```
  Scenario: Ruff 配置验证
    Tool: Bash
    Steps:
      1. uv run ruff check --select ALL .
    Expected: "All checks passed!" 或无错误
    Evidence: .sisyphus/evidence/task-02-ruff.txt
  ```

  **提交**: YES
  - Message: `chore: add ruff and mypy configuration`
  - Files: `ruff.toml, pyproject.toml`

- [x] 3. 基础类型定义

  **做什么**:
  - 创建 `src/ate_platform/__init__.py`
  - 创建 `src/ate_platform/types.py`
  - 定义核心类型：
    - `StepStatus` (Enum: PENDING, RUNNING, PASSED, FAILED, SKIPPED, ERROR)
    - `StepResult` (dataclass: status, outputs, error)
    - `Condition` (dataclass: step, status, expression, resource_available)
    - `VariableValue` (TypedDict)
  - 创建 `src/ate_platform/exceptions.py`
  - 定义自定义异常：
    - `StepTimeoutError`
    - `ConditionTimeoutError`
    - `ResourceAcquireError`
    - `ScriptExecutionError`

  **不能做**:
  - 不添加实现逻辑
  - 不引入外部依赖

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 1
  - **阻塞**: Task 5-18
  - **被阻塞**: Task 1

  **引用**:
  - OpenHTF 状态定义: `D:\chenjun\openhtf\openhtf\core\test_record.py` (Outcome enum)
  - OpenTAP Verdict: `D:\chenjun\opentap\Source\Tap\Verdict.cs`

  **验收标准**:
  - [ ] 类型导入无错误
  - [ ] mypy 类型检查通过

  **QA 场景**:
  ```
  Scenario: 类型定义验证
    Tool: Bash
    Steps:
      1. uv run python -c "from ate_platform.types import StepStatus; print(StepStatus.PENDING)"
    Expected: "StepStatus.PENDING"
    Evidence: .sisyphus/evidence/task-03-types.txt
  ```

  **提交**: YES
  - Message: `feat: add core type definitions and exceptions`
  - Files: `src/ate_platform/__init__.py, src/ate_platform/types.py, src/ate_platform/exceptions.py`

- [x] 4. 测试框架配置 (pytest)

  **做什么**:
  - 添加 pytest 依赖到 `pyproject.toml` [dev]
  - 添加 pytest-asyncio 依赖
  - 创建 `pytest.ini` 或 `pyproject.toml [tool.pytest]`
  - 配置 asyncio_mode = "auto"
  - 创建 `tests/conftest.py` 基础 fixtures
  - 创建测试目录结构：`tests/unit/`, `tests/integration/`

  **不能做**:
  - 不添加具体测试用例（在后续任务中添加）

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 1
  - **阻塞**: 后续测试任务
  - **被阻塞**: Task 1

  **引用**:
  - pytest-asyncio: `https://pytest-asyncio.readthedocs.io/`

  **验收标准**:
  - [ ] `uv run pytest --collect-only` 无错误
  - [ ] conftest.py 包含基础 fixtures

  **QA 场景**:
  ```
  Scenario: pytest 配置验证
    Tool: Bash
    Steps:
      1. uv run pytest --version
    Expected: "pytest x.x.x"
    Evidence: .sisyphus/evidence/task-04-pytest.txt
  ```

  **提交**: YES
  - Message: `chore: configure pytest with asyncio support`
  - Files: `pyproject.toml, pytest.ini, tests/conftest.py`

### Wave 2: 核心调度组件

- [x] 5. 事件总线 EventBus

  **做什么**:
  - 创建 `src/ate_platform/scheduler/event_bus.py`
  - 实现 `EventType` 枚举（STEP_STATUS_CHANGED, VARIABLE_CHANGED, RESOURCE_RELEASED, TIMER_EXPIRED, EXTERNAL_CMD）
  - 实现 `Event` dataclass（type, data, timestamp）
  - 实现 `EventBus` 类：
    - `subscribe(event_type, callback)`
    - `unsubscribe(event_type, callback)`
    - `async publish(event_type, data)`
    - `async start()` - 事件循环
    - `async stop()` - 优雅关闭
  - 使用 `asyncio.Queue` 作为事件队列
  - 支持通配符订阅（订阅所有事件）

  **不能做**:
  - 不添加业务逻辑回调
  - 不实现持久化（第一轮）

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 2
  - **阻塞**: Task 11 (ScannerScheduler)
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 692-729 行（事件总线设计）
  - Fixate pubsub: `D:\chenjun\Fixate\src\fixate\sequencer.py` (事件系统参考)

  **验收标准**:
  - [ ] 单元测试覆盖发布/订阅逻辑
  - [ ] 支持异步回调
  - [ ] 支持通配符订阅

  **QA 场景**:
  ```
  Scenario: 事件总线基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
import asyncio
from ate_platform.scheduler.event_bus import EventBus, EventType

async def test():
    bus = EventBus()
    received = []
    bus.subscribe(EventType.STEP_STATUS_CHANGED, lambda e: received.append(e))
    await bus.start()
    await bus.publish(EventType.STEP_STATUS_CHANGED, {'step_id': 'test'})
    await asyncio.sleep(0.1)
    await bus.stop()
    assert len(received) == 1
    print('OK')

asyncio.run(test())
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-05-eventbus.txt
  ```

  **提交**: YES
  - Message: `feat(scheduler): add event bus with asyncio queue`
  - Files: `src/ate_platform/scheduler/__init__.py, src/ate_platform/scheduler/event_bus.py, tests/unit/scheduler/test_event_bus.py`

- [x] 6. 变量空间 VariableSpace

  **做什么**:
  - 创建 `src/ate_platform/scheduler/variable_space.py`
  - 实现 `VariableSpace` 类：
    - `_scope: dict` - 序列级变量
    - `_steps: dict[str, dict]` - 步骤级变量（steps.<step_id>.<key>）
    - `_global: dict` - 全局变量（只读）
    - `get(name: str, default=None)` - 支持作用域解析
    - `set(name: str, value)` - 写入变量（带白名单校验）
    - `resolve(expression: str)` - 解析 ${scope.xxx} 语法
  - 实现线程安全（使用 `threading.Lock`）
  - 支持变量变更事件发布

  **不能做**:
  - 不支持嵌套作用域（简化为三级）
  - 不实现持久化

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 2
  - **阻塞**: Task 7, 11
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 829-843 行（ContextProxy 与变量）
  - OpenHTF TestState: `D:\chenjun\openhtf\openhtf\core\test_state.py`

  **验收标准**:
  - [ ] 支持 scope/steps/global 三级作用域
  - [ ] 线程安全读写
  - [ ] 变量解析正确

  **QA 场景**:
  ```
  Scenario: 变量空间基本操作
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.scheduler.variable_space import VariableSpace

vs = VariableSpace()
vs._scope['voltage'] = 3.3
vs._steps['step1'] = {'result': 'pass'}

assert vs.get('scope.voltage') == 3.3
assert vs.get('steps.step1.result') == 'pass'
assert vs.resolve('${scope.voltage}') == '3.3'
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-06-variable-space.txt
  ```

  **提交**: YES
  - Message: `feat(scheduler): add variable space with scope support`
  - Files: `src/ate_platform/scheduler/variable_space.py, tests/unit/scheduler/test_variable_space.py`

- [x] 7. 条件评估器 ConditionEvaluator

  **做什么**:
  - 创建 `src/ate_platform/scheduler/condition_evaluator.py`
  - 实现 `ConditionEvaluator` 类：
    - `evaluate(condition: Condition) -> bool`
    - 支持 `step.status` 条件检查
    - 支持 `expression` 表达式求值（使用 simpleeval）
    - 支持 `resource_available` 资源检查
    - 支持 `all`/`any` 逻辑组合
  - 添加超时等待逻辑（`wait_for_condition`）
  - 注册内置函数（`time_since`, `step_outputs`）

  **不能做**:
  - 不支持自定义函数注册（第一轮）
  - 不实现沙箱隔离（simpleeval 默认安全）

  **推荐 Agent**:
  - **Category**: `deep`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 2
  - **阻塞**: Task 11
  - **被阻塞**: Task 3, 6

  **引用**:
  - 方案文档: `实现方案.md` 第 577-611 行（前置条件规范）
  - simpleeval: `https://github.com/danthedecklace/simpleeval`

  **验收标准**:
  - [ ] 支持所有条件类型评估
  - [ ] 支持逻辑组合（all/any）
  - [ ] 单元测试覆盖率 > 90%

  **QA 场景**:
  ```
  Scenario: 条件评估基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.types import Condition

# 模拟状态
registry = type('Registry', (), {'get_status': lambda self, sid: 'passed'})()
vs = type('VarSpace', (), {'get': lambda self, n, d=None: 3.3})()
res_mgr = type('ResMgr', (), {'is_available': lambda self, r: True})()

evaluator = ConditionEvaluator(vs, registry, res_mgr)

cond = Condition(step='step1', status='passed')
assert evaluator.evaluate(cond) == True
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-07-condition-eval.txt
  ```

  **提交**: YES
  - Message: `feat(scheduler): add condition evaluator with simpleeval`
  - Files: `src/ate_platform/scheduler/condition_evaluator.py, tests/unit/scheduler/test_condition_evaluator.py`

- [x] 8. 资源管理器 ResourceManager

  **做什么**:
  - 创建 `src/ate_platform/scheduler/resource_manager.py`
  - 实现 `ResourceManager` 类：
    - `_locks: dict[str, threading.Lock]` - 资源锁映射
    - `_owners: dict[str, str]` - 资源持有者映射
    - `acquire(resource_id: str, owner_id: str, timeout: float = None) -> bool`
    - `release(resource_id: str, owner_id: str)`
    - `is_available(resource_id: str) -> bool`
    - `get_owner(resource_id: str) -> str | None`
  - 实现死锁检测（超时检测）
  - 发布 `RESOURCE_RELEASED` 事件

  **不能做**:
  - 不实现分布式锁（第一轮仅本地锁）
  - 不实现锁优先级

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 2
  - **阻塞**: Task 11, 14
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 846-851 行（资源锁管理）
  - TofuPilot ResourceManager: `D:\chenjun\framework\plugs\manager.rs`

  **验收标准**:
  - [ ] 线程安全获取/释放
  - [ ] 支持超时等待
  - [ ] 死锁检测（超时报错）

  **QA 场景**:
  ```
  Scenario: 资源管理基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.scheduler.resource_manager import ResourceManager

rm = ResourceManager()
assert rm.acquire('DMM_CH1', 'step1', timeout=1.0) == True
assert rm.is_available('DMM_CH1') == False
assert rm.acquire('DMM_CH1', 'step2', timeout=0.1) == False  # 已被占用
rm.release('DMM_CH1', 'step1')
assert rm.is_available('DMM_CH1') == True
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-08-resource-mgr.txt
  ```

  **提交**: YES
  - Message: `feat(scheduler): add resource manager with local locks`
  - Files: `src/ate_platform/scheduler/resource_manager.py, tests/unit/scheduler/test_resource_manager.py`

- [x] 9. YAML DSL 解析器

  **做什么**:
  - 创建 `src/ate_platform/dsl/parser.py`
  - 定义 YAML Schema（参考方案文档第 555-688 行）
  - 实现 `YamlPlan` dataclass（name, version, scope, max_concurrency, steps）
  - 实现 `YamlStep` dataclass（id, script, params, preconditions, resources, timeout, retry, on_fail）
  - 实现 `YamlParser` 类：
    - `parse(yaml_str: str) -> YamlPlan`
    - `validate(plan: YamlPlan) -> list[ValidationError]`
  - 添加 PyYAML 依赖
  - 实现循环展开（LoopCompiler）

  **不能做**:
  - 不实现 Schema 迁移（第一轮固定 v3.0）
  - 不添加 DSL 扩展特性

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 2
  - **阻塞**: Task 10, 19
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 555-688 行（DSL 规范）
  - TofuPilot Procedure Schema: `D:\chenjun\framework\procedure\schema.rs`

  **验收标准**:
  - [ ] 正确解析示例 YAML
  - [ ] 验证必填字段
  - [ ] 循环展开正确

  **QA 场景**:
  ```
  Scenario: YAML 解析基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.dsl.parser import YamlParser

yaml_str = '''
name: Test Sequence
version: '3.0'
steps:
  - id: step1
    script: test.py
'''
parser = YamlParser()
plan = parser.parse(yaml_str)
assert plan.name == 'Test Sequence'
assert len(plan.steps) == 1
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-09-yaml-parser.txt
  ```

  **提交**: YES
  - Message: `feat(dsl): add YAML parser with validation`
  - Files: `src/ate_platform/dsl/__init__.py, src/ate_platform/dsl/parser.py, tests/unit/dsl/test_parser.py`

### Wave 3: 执行器和驱动

- [x] 10. 步骤注册表 StepRegistry

  **做什么**:
  - 创建 `src/ate_platform/scheduler/step_registry.py`
  - 实现 `StepRegistry` 类：
    - `_steps: dict[str, StepStatus]` - 步骤状态映射
    - `_conditions: dict[str, Condition]` - 前置条件映射
    - `register(step_id: str, condition: Condition)`
    - `update_status(step_id: str, status: StepStatus)`
    - `get_status(step_id: str) -> StepStatus`
    - `get_ready_steps() -> list[str]` - 获取可执行步骤
  - 发布 `STEP_STATUS_CHANGED` 事件

  **不能做**:
  - 不执行步骤（仅状态管理）
  - 不实现持久化

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 3
  - **阻塞**: Task 11
  - **被阻塞**: Task 9

  **引用**:
  - 方案文档: `实现方案.md` 第 732-761 行（状态管理）
  - OpenHTF TestDescriptor: `D:\chenjun\openhtf\openhtf\core\test_descriptor.py`

  **验收标准**:
  - [ ] 状态查询正确
  - [ ] 条件满足时返回就绪步骤

  **QA 场景**:
  ```
  Scenario: 步骤注册表基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.types import StepStatus, Condition

registry = StepRegistry()
registry.register('step1', Condition())
registry.update_status('step1', StepStatus.PASSED)
assert registry.get_status('step1') == StepStatus.PASSED
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-10-step-registry.txt
  ```

  **提交**: YES
  - Message: `feat(scheduler): add step registry for status tracking`
  - Files: `src/ate_platform/scheduler/step_registry.py, tests/unit/scheduler/test_step_registry.py`

- [x] 11. 扫描调度器 ScannerScheduler

  **做什么**:
  - 创建 `src/ate_platform/scheduler/scanner_scheduler.py`
  - 实现 `ScannerScheduler` 类（核心创新）：
    - `_event_bus: EventBus`
    - `_registry: StepRegistry`
    - `_evaluator: ConditionEvaluator`
    - `_variable_space: VariableSpace`
    - `_resource_manager: ResourceManager`
    - `_scan_interval: float = 0.1` - 扫描间隔（100ms）
    - `async start()` - 启动扫描循环
    - `async stop()` - 停止扫描
    - `async _scan()` - 扫描所有步骤条件
  - 订阅事件：`VARIABLE_CHANGED`, `STEP_STATUS_CHANGED`, `RESOURCE_RELEASED`
  - 实现条件满足检测和步骤就绪通知
  - 实现死锁检测（循环扫描无法推进时报警）

  **不能做**:
  - 不直接执行步骤（由 Executor 执行）
  - 不实现分布式调度

  **推荐 Agent**:
  - **Category**: `deep`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: NO
  - **阻塞**: Task 19
  - **被阻塞**: Task 5, 6, 7, 8, 10

  **引用**:
  - 方案文档: `实现方案.md` 第 692-731 行（扫描式调度器）
  - OpenHTF TestExecutor: `D:\chenjun\openhtf\openhtf\core\test_executor.py`

  **验收标准**:
  - [ ] 条件满足时触发就绪事件
  - [ ] 扫描延迟 < 200ms（95%）
  - [ ] 死锁检测工作正常

  **QA 场景**:
  ```
  Scenario: 扫描调度器基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
import asyncio
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.event_bus import EventBus
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.types import Condition

async def test():
    event_bus = EventBus()
    registry = StepRegistry()
    scheduler = ScannerScheduler(event_bus, registry)
    
    registry.register('step1', Condition())
    await scheduler.start()
    await asyncio.sleep(0.2)  # 等待扫描
    await scheduler.stop()
    print('OK')

asyncio.run(test())
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-11-scanner-scheduler.txt
  ```

  **提交**: YES
  - Message: `feat(scheduler): add scanner scheduler with event-driven dispatch`
  - Files: `src/ate_platform/scheduler/scanner_scheduler.py, tests/unit/scheduler/test_scanner_scheduler.py`

- [x] 12. ContextProxy 上下文代理

  **做什么**:
  - 创建 `src/ate_platform/executor/context_proxy.py`
  - 实现 `ContextProxy` 类（脚本上下文访问代理）：
    - `_variable_space: VariableSpace`
    - `_resource_manager: ResourceManager`
    - `_step_id: str`
    - `_outputs: dict` - 步骤输出
    - `__getitem__(name: str)` - 变量读取代理
    - `__setitem__(name: str, value)` - 变量写入代理（带白名单）
    - `resource(resource_id: str)` - 资源访问代理
    - `log(level: str, message: str)` - 日志记录
  - 实现 `@measure` 装饰器（输出变量声明）

  **不能做**:
  - 不直接暴露 VariableSpace（通过代理访问）
  - 不允许写入全局变量

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 3
  - **阻塞**: Task 13
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 829-851 行（ContextProxy 设计）
  - OpenHTF PhaseContext: `D:\chenjun\openhtf\openhtf\core\test_state.py`

  **验收标准**:
  - [ ] 变量读写正确代理
  - [ ] 资源访问正确代理
  - [ ] 白名单校验工作

  **QA 场景**:
  ```
  Scenario: ContextProxy 基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.executor.context_proxy import ContextProxy

vs = type('VarSpace', (), {'get': lambda self, n, d=None: 'test_value', 'set': lambda self, n, v: None})()
proxy = ContextProxy(vs, None, 'step1')
assert proxy['scope.test'] == 'test_value'
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-12-context-proxy.txt
  ```

  **提交**: YES
  - Message: `feat(executor): add context proxy for script isolation`
  - Files: `src/ate_platform/executor/__init__.py, src/ate_platform/executor/context_proxy.py, tests/unit/executor/test_context_proxy.py`

- [x] 13. 进程执行器 ProcessExecutor

  **做什么**:
  - 创建 `src/ate_platform/executor/process_executor.py`
  - 实现 `ProcessExecutor` 类：
    - `_pool: multiprocessing.Pool`
    - `_max_workers: int = 4`
    - `_script_timeout: float = 60.0` - 默认超时
    - `execute(script_path: str, params: dict, timeout: float = None) -> StepResult`
    - `_run_script(script_path: str, params: dict) -> dict` - 子进程入口
    - `cancel(step_id: str)` - 取消执行
  - 使用 `multiprocessing.Pool` 进程池
  - 实现超时熔断（`timeout` 参数）
  - 实现异常捕获和结果封装
  - 发布 `STEP_STATUS_CHANGED` 事件

  **不能做**:
  - 不使用线程池（必须进程隔离）
  - 不实现热重载

  **推荐 Agent**:
  - **Category**: `deep`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 3
  - **阻塞**: Task 19
  - **被阻塞**: Task 3, 12

  **引用**:
  - 方案文档: `实现方案.md` 第 762-827 行（执行器设计）
  - OpenHTF KillableThread: `D:\chenjun\openhtf\openhtf\core\util\threads.py`
  - MATS ExternalHardwareAllocator: `D:\chenjun\MATS\src\mats\hardware\allocator.py`

  **验收标准**:
  - [ ] 脚本在独立进程执行
  - [ ] 超时正确终止
  - [ ] 异常正确捕获

  **QA 场景**:
  ```
  Scenario: 进程执行器基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.executor.process_executor import ProcessExecutor

executor = ProcessExecutor(max_workers=1)
result = executor.execute('examples/test_pass.py', {}, timeout=5.0)
assert result.status.value == 'passed'
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-13-process-executor.txt
  ```

  **提交**: YES
  - Message: `feat(executor): add process executor with timeout handling`
  - Files: `src/ate_platform/executor/process_executor.py, tests/unit/executor/test_process_executor.py`

- [x] 14. PyVISA 驱动基类

  **做什么**:
  - 创建 `src/ate_platform/drivers/__init__.py`
  - 创建 `src/ate_platform/drivers/base.py`
  - 添加 `pyvisa-py` 依赖
  - 实现 `InstrumentDriver` 基类：
    - `_resource_manager: pyvisa.ResourceManager`
    - `_instrument: pyvisa.Resource | None`
    - `_address: str`
    - `_lock: threading.Lock`
    - `connect(address: str)` - 连接仪器
    - `disconnect()` - 断开仪器
    - `write(command: str)` - 发送命令
    - `query(command: str, delay: float = 0.1) -> str` - 查询命令
    - `read() -> str` - 读取响应
  - 实现 `DriverRegistry` 注册表：
    - `register_driver(name: str, driver_class: type)`
    - `get_driver(name: str) -> InstrumentDriver`

  **不能做**:
  - 不实现具体仪器驱动（Task 15）
  - 不实现自动发现（手动注册）

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 3
  - **阻塞**: Task 15
  - **被阻塞**: Task 1, 8

  **引用**:
  - 方案文档: `实现方案.md` 第 852-894 行（仪器驱动架构）
  - PyVISA 文档: `https://pyvisa.readthedocs.io/`
  - OpenTAP IInstrument: `D:\chenjun\opentap\Source\Tap\IInstrument.cs`

  **验收标准**:
  - [ ] 基类方法完整
  - [ ] 连接/断开正常
  - [ ] 锁机制正确

  **QA 场景**:
  ```
  Scenario: PyVISA 基类基本功能（Mock）
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.drivers.base import InstrumentDriver

class MockDriver(InstrumentDriver):
    def connect(self, address: str):
        self._address = address
        self._connected = True

driver = MockDriver()
driver.connect('TCPIP::localhost::5025::SOCKET')
assert driver._connected == True
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-14-pyvisa-base.txt
  ```

  **提交**: YES
  - Message: `feat(drivers): add PyVISA instrument driver base class`
  - Files: `src/ate_platform/drivers/__init__.py, src/ate_platform/drivers/base.py, tests/unit/drivers/test_base.py`

- [x] 15. 示例仪器驱动（DMM, PSU）

  **做什么**:
  - 创建 `src/ate_platform/drivers/examples/`
  - 实现 `DMMDriver` (数字万用表):
    - `measure_voltage(channel: int = 1) -> float`
    - `measure_current(channel: int = 1) -> float`
    - `measure_resistance(channel: int = 1) -> float`
    - 支持常见 SCPI 命令
  - 实现 `PSUDriver` (可编程电源):
    - `set_voltage(channel: int, voltage: float)`
    - `set_current_limit(channel: int, current: float)`
    - `output_on(channel: int = 1)`
    - `output_off(channel: int = 1)`
    - `measure_current(channel: int = 1) -> float`
  - 注册到 DriverRegistry
  - 创建 Mock 版本用于测试

  **不能做**:
  - 不支持所有仪器型号（仅示例）
  - 不实现复杂校准

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 3
  - **阻塞**: Task 20
  - **被阻塞**: Task 14

  **引用**:
  - 方案文档: `实现方案.md` 第 895-950 行（驱动示例）
  - SCPI 参考: `https://www.ivifoundation.org/downloads/General/SCPI-99.PDF`

  **验收标准**:
  - [ ] 驱动可连接 Mock 仪器
  - [ ] 命令执行正确
  - [ ] 已注册到 Registry

  **QA 场景**:
  ```
  Scenario: 示例驱动基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
from ate_platform.drivers.examples.dmm import MockDMMDriver

dmm = MockDMMDriver()
dmm.connect('MOCK::DMM')
voltage = dmm.measure_voltage()
assert isinstance(voltage, float)
print('OK')
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-15-example-drivers.txt
  ```

  **提交**: YES
  - Message: `feat(drivers): add DMM and PSU example drivers`
  - Files: `src/ate_platform/drivers/examples/__init__.py, src/ate_platform/drivers/examples/dmm.py, src/ate_platform/drivers/examples/psu.py, tests/unit/drivers/test_examples.py`

### Wave 4: 数据层和集成

- [x] 16. SQLite 缓存管理器

  **做什么**:
  - 创建 `src/ate_platform/data/cache.py`
  - 添加 `aiosqlite` 依赖
  - 实现 `SQLiteCache` 类：
    - `_db_path: str`
    - `_db: aiosqlite.Connection | None`
    - `_lock: asyncio.Lock`
    - `async connect()` - 连接数据库
    - `async close()` - 关闭连接
    - `async save_result(step_id: str, result: StepResult)` - 保存结果
    - `async get_result(step_id: str) -> StepResult | None` - 获取结果
    - `async get_sequence_results(sequence_id: str) -> list[StepResult]`
  - 创建表结构：
    - `results` (id, sequence_id, step_id, status, outputs, error, timestamp)
    - `upload_queue` (id, payload, retry_count, created_at)
  - 启用 WAL 模式
  - 实现 WAL checkpoint（每 1000 页）

  **不能做**:
  - 不实现数据迁移（第一轮）
  - 不实现压缩

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 4
  - **阻塞**: Task 18, 19
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 951-981 行（数据缓存）
  - SQLite WAL: `https://www.sqlite.org/wal.html`

  **验收标准**:
  - [ ] 数据正确保存和读取
  - [ ] WAL 模式启用
  - [ ] 线程安全

  **QA 场景**:
  ```
  Scenario: SQLite 缓存基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
import asyncio
from ate_platform.data.cache import SQLiteCache
from ate_platform.types import StepResult, StepStatus

async def test():
    cache = SQLiteCache(':memory:')
    await cache.connect()
    result = StepResult(status=StepStatus.PASSED, outputs={}, error=None)
    await cache.save_result('step1', result)
    loaded = await cache.get_result('step1')
    assert loaded.status == StepStatus.PASSED
    await cache.close()
    print('OK')

asyncio.run(test())
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-16-sqlite-cache.txt
  ```

  **提交**: YES
  - Message: `feat(data): add SQLite cache with WAL mode`
  - Files: `src/ate_platform/data/__init__.py, src/ate_platform/data/cache.py, tests/unit/data/test_cache.py`

- [x] 17. NATS Publisher

  **做什么**:
  - 创建 `src/ate_platform/data/publisher.py`
  - 添加 `nats-py` 依赖
  - 实现 `NATSPublisher` 类：
    - `_servers: list[str]`
    - `_nc: nats.NATS | None`
    - `_js: nats.JetStreamContext | None`
    - `_stream_name: str = "ate_results"`
    - `_reconnect_backoff: list[float] = [1, 2, 5, 10, 30]` - 重连退避
    - `async connect()` - 连接 NATS
    - `async close()` - 关闭连接
    - `async publish(subject: str, payload: bytes)` - 发布消息
    - `async create_stream()` - 创建 JetStream
  - 实现自动重连（指数退避 1s→30s）
  - 实现消息确认机制

  **不能做**:
  - 不实现消费逻辑（仅发布）
  - 不实现消息压缩

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 4
  - **阻塞**: Task 18, 19
  - **被阻塞**: Task 1, 3

  **引用**:
  - 方案文档: `实现方案.md` 第 982-1010 行（NATS 上传）
  - NATS Python: `https://github.com/nats-io/nats.py`

  **验收标准**:
  - [ ] 消息正确发布
  - [ ] 重连机制工作
  - [ ] JetStream 创建成功

  **QA 场景**:
  ```
  Scenario: NATS Publisher 基本功能（Mock）
    Tool: Bash
    Steps:
      1. uv run python -c "
import asyncio
from ate_platform.data.publisher import NATSPublisher

async def test():
    # Mock 模式：不实际连接
    pub = NATSPublisher(['nats://localhost:4222'])
    print('OK')

asyncio.run(test())
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-17-nats-publisher.txt
  ```

  **提交**: YES
  - Message: `feat(data): add NATS publisher with reconnect backoff`
  - Files: `src/ate_platform/data/publisher.py, tests/unit/data/test_publisher.py`

- [x] 18. 断点续传逻辑

  **做什么**:
  - 创建 `src/ate_platform/data/resume.py`
  - 实现 `ResumeManager` 类：
    - `_cache: SQLiteCache`
    - `_publisher: NATSPublisher`
    - `_pending: asyncio.Queue`
    - `_running: bool = False`
    - `async start()` - 启动上传协程
    - `async stop()` - 停止上传
    - `async upload_result(result: StepResult)` - 上传单个结果
    - `async retry_pending()` - 重试队列中失败的消息
    - `async recover()` - 从 upload_queue 恢复未上传消息
  - 实现：
    - 成功上传后从队列删除
    - 失败后加入重试队列（最多 3 次）
    - 启动时恢复未上传消息

  **不能做**:
  - 不实现批量上传优化
  - 不实现消息优先级

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 4
  - **阻塞**: Task 19
  - **被阻塞**: Task 16, 17

  **引用**:
  - 方案文档: `实现方案.md` 第 1011-1045 行（断点续传）

  **验收标准**:
  - [ ] 消息正确上传
  - [ ] 失败重试工作
  - [ ] 启动恢复工作

  **QA 场景**:
  ```
  Scenario: 断点续传基本功能
    Tool: Bash
    Steps:
      1. uv run python -c "
import asyncio
from ate_platform.data.resume import ResumeManager

async def test():
    # Mock 模式
    manager = ResumeManager(None, None)
    print('OK')

asyncio.run(test())
"
    Expected: "OK"
    Evidence: .sisyphus/evidence/task-18-resume.txt
  ```

  **提交**: YES
  - Message: `feat(data): add resume manager with retry logic`
  - Files: `src/ate_platform/data/resume.py, tests/unit/data/test_resume.py`

- [x] 19. 集成测试：完整流程

  **做什么**:
  - 创建 `tests/integration/test_full_flow.py`
  - 测试完整流程：
    1. 解析 YAML 测试计划
    2. 注册步骤和条件
    3. 启动扫描调度器
    4. 条件满足后触发执行
    5. 执行器运行脚本
    6. 结果保存到 SQLite
    7. 消息发布到 NATS（Mock）
  - 创建示例 YAML 测试计划
  - 创建示例 Python 脚本（Mock 仪器）
  - 验证：
    - 调度延迟 < 200ms（95%）
    - 结果正确保存
    - 消息正确发布

  **不能做**:
  - 不使用真实仪器（Mock）
  - 不测试云侧服务

  **推荐 Agent**:
  - **Category**: `unspecified-high`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: NO
  - **阻塞**: Task F3
  - **被阻塞**: Task 11, 13, 18

  **引用**:
  - 方案文档: `实现方案.md` 完整架构
  - OpenHTF integration tests: `D:\chenjun\openhtf\tests\`

  **验收标准**:
  - [ ] 完整流程通过
  - [ ] 调度延迟达标
  - [ ] 无资源泄漏

  **QA 场景**:
  ```
  Scenario: 完整流程集成测试
    Tool: Bash
    Steps:
      1. uv run pytest tests/integration/test_full_flow.py -v
    Expected: "passed"
    Evidence: .sisyphus/evidence/task-19-integration.txt
  ```

  **提交**: YES
  - Message: `test: add integration test for full workflow`
  - Files: `tests/integration/test_full_flow.py, tests/fixtures/sample_plan.yaml, tests/fixtures/test_scripts/`

- [x] 20. 示例测试脚本

  **做什么**:
  - 创建 `examples/scripts/`
  - 创建示例脚本：
    - `voltage_test.py` - 电压测试（使用 DMM 驱动）
    - `current_test.py` - 电流测试
    - `power_on_test.py` - 上电测试（使用 PSU 驱动）
  - 每个脚本包含：
    - `@measure` 装饰器声明输出
    - `main(context: ContextProxy)` 入口函数
    - 使用 ContextProxy 访问变量和资源
  - 创建 `examples/run_test.py` - 独立运行脚本
  - 创建 `examples/README.md` - 使用说明

  **不能做**:
  - 不包含复杂测试逻辑（仅示例）
  - 不依赖真实仪器（支持 Mock 模式）

  **推荐 Agent**:
  - **Category**: `quick`
  - **Skills**: `[]`

  **并行化**:
  - **可并行**: YES
  - **并行组**: Wave 4
  - **阻塞**: Task 19
  - **被阻塞**: Task 15

  **引用**:
  - 方案文档: `实现方案.md` 第 555-688 行（DSL 示例）
  - OpenHTF examples: `D:\chenjun\openhtf\examples\`

  **验收标准**:
  - [ ] 脚本可独立运行
  - [ ] Mock 模式工作
  - [ ] README 清晰

  **QA 场景**:
  ```
  Scenario: 示例脚本运行
    Tool: Bash
    Steps:
      1. cd examples && uv run python run_test.py --mock voltage_test.py
    Expected: "PASSED"
    Evidence: .sisyphus/evidence/task-20-examples.txt
  ```

  **提交**: YES
  - Message: `feat: add example test scripts with mock mode`
  - Files: `examples/scripts/*.py, examples/run_test.py, examples/README.md`

---

## Final Verification Wave

- [x] F1. **计划合规审计** — `oracle`
  读取计划全文。验证每个"必须有"已实现，每个"必须没有"未出现。检查证据文件存在。对比交付物与计划。

- [x] F2. **代码质量审查** — `unspecified-high`
  运行 `ruff check` + `mypy` + `pytest`。检查所有变更文件：`as any`/`@ts-ignore`、空 catch、console.log、注释代码、未使用导入。检测 AI slop。

- [x] F3. **手动 QA 测试** — `unspecified-high`
  从干净状态开始。执行每个 QA 场景。测试跨任务集成。测试边缘情况：空状态、无效输入、快速操作。

- [x] F4. **范围一致性检查** — `deep`
  对每个任务：读取"做什么"，读取实际 diff。验证 1:1 对应。检测跨任务污染。

---

## Commit Strategy

- **Wave 1**: `chore: init project structure` - pyproject.toml, .python-version, ruff.toml
- **Wave 2**: `feat(scheduler): add event bus and condition evaluator`
- **Wave 3**: `feat(executor): add process executor and pyvisa driver base`
- **Wave 4**: `feat(data): add sqlite cache and nats publisher`
- **Final**: `test: add integration tests and examples`

---

## Success Criteria

### 验证命令
```bash
# 代码质量
uv run ruff check src/
uv run mypy src/

# 测试
uv run pytest tests/ -v

# 示例运行
uv run python examples/run_test.py --mock
```

### 最终检查清单
- [x] 所有"必须有"已实现
- [x] 所有"必须没有"未出现
- [x] 测试覆盖率 > 80%（核心模块）
- [x] 示例测试可运行
- [x] 无 Ruff/mypy 错误