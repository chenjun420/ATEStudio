# Draft: Loop Containers + Monaco Editor + Real-time Execution Status

## Requirements (confirmed)
- **Feature 1: Loop Container Nodes** — Visual loop containers in the sequence editor (for/while/foreach)
- **Feature 3: Monaco Editor Integration** — Script editing with syntax highlighting in the frontend
- **Feature 4: Real-time Execution Status** — Live test execution monitoring via SSE (pending user confirmation)
- User explicitly requested evaluation of real-time communication technology against latest best practices

## Technical Decisions
- **Real-time communication**: SSE via FastAPI EventSourceResponse (RECOMMENDED by research)
  - Rationale: Unidirectional fit, FastAPI native, auto-reconnect, Last-Event-ID resumption, natural NATS bridge, ~1hr implementation
  - WebSocket over-engineered (bidirectional not needed), NATS WS security risk, GraphQL overkill, WebTransport requires HTTP/3
  - Control commands (start/stop/abort) stay on REST POST endpoints
  - PENDING: User confirmation on SSE choice

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
- Need: new /scripts route with ScriptManager view, or modal/drawer from StepLibraryPanel

### Execution Model Assessment (Critical for Loop Containers)
- Serial execution: SUPPORTED (precondition chains + blocking execute())
- Parallel execution: PARTIALLY SUPPORTED (Pool exists, no fan-out dispatch, max_concurrency unused)
- **Loop containers with parallel iterations WILL NOT WORK currently** — 3 blockers:
  1. No parallel dispatch mechanism (execute() is synchronous-blocking)
  2. VariableSpace not cross-process safe (threading.Lock, not multiprocessing-safe)
  3. No barrier/join primitive
- Event bus: PARTIALLY SUPPORTED (VARIABLE_CHANGED/RESOURCE_RELEASED never fired, inconsistent schemas)
- Missing shared types: ExecutionMode, StepType, ParallelGroup, LoopContainer, SubFlowRef

## User Answers (Round 1 → Confirmed)
- **实时通讯**: SSE — 单一方案，简单可靠
- **循环容器范围**: 支持串行+并行迭代，用户在属性面板配置执行模式
- **循环容器可视化**: 嵌入式子图 — 双击进入子图编辑内部步骤
- **循环容器序列化**: 嵌套YAML结构 — loop内有自己的steps列表
- **脚本内容存储**: 新增文件内容API (GET/PUT /api/v1/scripts/{id}/content) + Git版本化管理
- **Monaco编辑器位置**: 属性面板内 或 单独弹出对话框

## Open Questions
- [x] Test strategy: 实现后补测试
- [x] Parallel iteration execution model: 线程+asyncio（共享VariableSpace，GIL限制可接受）
- [x] Git版本化: GitPython库（不依赖系统git）
- [x] 嵌入式子图交互: 面包屑导航（主图 > 循环容器名，点击返回）
- [x] SSE确认: SSE via EventSourceResponse，Streamable HTTP不适用
- [ ] SSE事件粒度和类型定义
- [ ] Monaco编辑器: 属性面板内嵌 vs 弹出对话框的具体触发方式
- [ ] 循环容器DSL详细设计（for/while/foreach条件语法）

## Scope Boundaries
- INCLUDE: Loop container nodes (visual + serialization + execution), Monaco editor, SSE real-time status
- EXCLUDE: AI-assisted generation, Qdrant integration, deployment/CI/CD, frontend testing framework setup
