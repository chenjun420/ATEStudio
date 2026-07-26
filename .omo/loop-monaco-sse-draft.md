# Draft: Loop Containers + Monaco Editor + Real-time Execution Status

## Requirements (confirmed)
- **Feature 1: Loop Container Nodes** — Visual loop containers in the sequence editor (for/while/foreach)
- **Feature 3: Monaco Editor Integration** — Script editing with syntax highlighting in the frontend
- **Feature 4: Real-time Execution Status** — Live test execution monitoring via SSE
- User explicitly requested evaluation of real-time communication technology against latest best practices

## Technical Decisions
- **Real-time communication**: SSE via FastAPI EventSourceResponse
  - Rationale: Unidirectional fit, FastAPI native, auto-reconnect, Last-Event-ID resumption, natural NATS bridge
  - WebSocket over-engineered, NATS WS security risk, GraphQL overkill, WebTransport requires HTTP/3
  - Streamable HTTP is MCP-specific, not applicable (same wire format as SSE but less stable spec)
  - Control commands (start/stop/abort) stay on REST POST endpoints

## Research Findings

### Node System Architecture (for Loop Containers)
- 6 X6 shapes registered, `script-step-node` is primary (180x80, input/output ports)
- Vue components (ScriptStepNode.vue, VariableNode.vue) are DEAD CODE — @antv/x6-vue-shape installed but unused
- NodeGroup is sidebar-only concept (localStorage), NOT a visual canvas container
- YAML DSL v3.0 is flat steps with preconditions — no hierarchical/loop support
- Cycle detection BLOCKS loop back-edges — must be modified
- Port model is simple (input/output only) — loop containers need loop-back ports
- PropertyPanel only handles ScriptStepData/VariableData — needs LoopContainerData branch
- X6 3.x supports parent/children on cells for grouping — currently unused

### Monaco Editor Integration
- Frontend script API is READ-ONLY — no create/update/delete methods (backend supports full CRUD)
- Backend stores `script_path` (filesystem path), NOT content — need new API endpoint or DB field
- Zero code editor components exist — no Monaco, CodeMirror, or Ace
- Settings page is minimal (dark mode + language) — NOT appropriate for script editing

### Execution Model Assessment (Critical for Loop Containers)
- Serial execution: SUPPORTED (precondition chains + blocking execute())
- Parallel execution: PARTIALLY SUPPORTED (Pool exists, no fan-out dispatch, max_concurrency unused)
- **Loop containers with parallel iterations WILL NOT WORK currently** — 3 blockers:
  1. No parallel dispatch mechanism (execute() is synchronous-blocking)
  2. VariableSpace not cross-process safe (threading.Lock, not multiprocessing-safe)
  3. No barrier/join primitive
- Event bus: PARTIALLY SUPPORTED (VARIABLE_CHANGED/RESOURCE_RELEASED never fired, inconsistent schemas)
- Missing shared types: ExecutionMode, StepType, ParallelGroup, LoopContainer, SubFlowRef

### Free-Threaded Python Assessment
- NOT recommended: experimental status, PyVISA C-extension risk, I/O-bound workload doesn't benefit
- threading+asyncio sufficient for I/O-bound parallel iterations under GIL
- Plan: design executor abstraction interface for future free-threaded migration

## User Answers (All Confirmed)
- **实时通讯**: SSE — 单一方案，简单可靠
- **循环容器范围**: 支持串行+并行迭代，用户在属性面板配置执行模式
- **循环容器可视化**: 嵌入式子图 — 双击进入子图编辑内部步骤
- **循环容器序列化**: 嵌套YAML结构 — loop内有自己的steps列表
- **脚本内容存储**: 新增文件内容API (GET/PUT /api/v1/scripts/{id}/content) + Git版本化管理
- **Monaco编辑器位置**: 属性面板内 或 单独弹出对话框
- **测试策略**: 实现后补测试
- **并行迭代执行模型**: 线程+asyncio（共享VariableSpace，GIL限制可接受）
- **Git版本化**: GitPython库
- **子图交互**: 面包屑导航（主图 > 循环容器名，点击返回）
- **Free-threaded Python**: 不采用，但设计执行器抽象接口预留迁移路径
- **跨平台**: 必须兼容 ARM64/x86_64 × Linux/Windows

## Cross-Platform Constraint
- **Target platforms**: ARM64/x86_64 × Linux/Windows (4 platforms)
- **Impact on execution model**: threading+asyncio SIMPLIFIES cross-platform — no multiprocessing spawn/fork differences
- **PyVISA strategy**: Default to pyvisa-py (pure Python, cross-platform), NI-VISA as optional backend
- **Signal handling**: Thread model uses threading.Event (cross-platform), no POSIX signal dependency
- **DB drivers**: asyncpg (C ext, ARM64 wheel check needed) + aiomysql (pure Python) + aiosqlite (pure Python); consider psycopg as fallback
- **File paths**: Enforce pathlib.Path everywhere, no hardcoded separators
- **CI requirement**: Test on all 4 platforms (GitHub Actions + self-hosted ARM Windows runner)

## Scope Boundaries
- INCLUDE: Loop container nodes (visual + serialization + execution), Monaco editor, SSE real-time status
- EXCLUDE: AI-assisted generation, Qdrant integration, deployment/CI/CD, frontend testing framework setup

## Open Questions (auto-resolved with defaults)
- SSE事件粒度: 按执行ID推送，包含步骤级事件 (STEP_STARTED, STEP_COMPLETED, STEP_FAILED, EXECUTION_STARTED, EXECUTION_COMPLETED)
- Monaco触发方式: 属性面板内嵌代码预览 + 点击"编辑"按钮弹出全屏对话框
- 循环容器DSL: for/while/foreach三种，条件用Python表达式语法
