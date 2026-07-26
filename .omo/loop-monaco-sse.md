# Plan: Loop Containers + Monaco Editor + Real-time Execution Status

## Overview

Three features for ATEStudio that share foundational infrastructure:
1. **Loop Container Nodes** — Visual loop containers (for/while/foreach) with embedded sub-graphs, nested YAML serialization, and serial/parallel iteration execution
2. **Monaco Editor Integration** — Python script editing with syntax highlighting, Git-based versioning
3. **SSE Real-time Execution Status** — Live test execution monitoring via FastAPI EventSourceResponse

**Critical insight**: These features share 4 broken foundations that MUST be fixed first (Phase 0), or each feature will fight the codebase at every step.

## User Decisions

| Decision | Choice |
|---|---|
| Real-time communication | SSE via FastAPI EventSourceResponse |
| Loop iteration mode | User-configured (serial or parallel) |
| Loop container visual | Embedded sub-graph (double-click to enter, breadcrumb navigation) |
| Loop serialization | Nested YAML structure |
| Script content storage | File content API + GitPython versioning |
| Monaco editor placement | PropertyPanel inline + popup dialog |
| Parallel execution model | Threading + asyncio (not multiprocessing) |
| Test strategy | Tests after implementation |
| Cross-platform | ARM64/x86_64 × Linux/Windows |

## Architecture

### Data Flow (SSE)
```
Executor → EventBus → NATSPublisher → JetStream → NATSSubscriber → asyncio.Queue → SSE endpoint → Vue 3 EventSource
                                                                                                              ↓
                                                                                                    useEventSource composable
                                                                                                              ↓
                                                                                                    GraphContainer (node status updates)
```

### Loop Container Model
```
YamlPlan
├── name, version, scope, max_concurrency
└── steps: list[YamlStep | YamlLoop]

YamlLoop (NEW)
├── id: str
├── loop_type: "for" | "while" | "foreach"
├── condition: str                    # while condition or for range expression
├── iteration_var: str | None         # e.g. "i" or "item"
├── collection_expr: str | None       # e.g. "scope.channels" for foreach
├── count: int | None                 # for fixed-count loops
├── execution_mode: "serial" | "parallel"
├── max_concurrency: int              # for parallel mode
├── steps: list[YamlStep | YamlLoop]  # NESTED — loop body
├── resources: dict
├── timeout: int
└── on_fail: str | None
```

### Script Content API
```
GET  /api/v1/scripts/{id}/content     → ScriptContentResponse { content, version, last_modified }
PUT  /api/v1/scripts/{id}/content     → ScriptContentUpdate { content, commit_message? }
GET  /api/v1/scripts/{id}/versions    → ScriptVersionListResponse { versions: [...] }
GET  /api/v1/scripts/{id}/versions/{hash} → ScriptContentResponse (specific version)
```

---

## Phase 0: Fix Foundations (4 tasks)

### Task 0.1: Unify YAML DSL Schema

**Why**: Frontend and backend YAML schemas already diverge (frontend has `export_outputs`, backend has `retry`; `scope` type mismatch). Adding loops to both independently will create irreconcilable schemas.

**What**:
1. Extend `src/shared/dsl.py` as the single canonical schema:
   - Add `YamlLoop` dataclass with nested `steps: list[YamlStep | YamlLoop]`
   - Add `LoopType` enum: `FOR`, `WHILE`, `FOREACH`
   - Add `ExecutionMode` enum: `SERIAL`, `PARALLEL`
   - Update `YamlPlan.steps` to `list[YamlStep | YamlLoop]`
   - Add `export_outputs: bool = False` to `YamlStep` (currently frontend-only)
   - Change `YamlPlan.scope` from `str` to `dict[str, Any]` (match frontend)
   - Add `StepType` enum: `SCRIPT`, `LOOP`, `CALL` (future-proof)
2. Add YAML → Python parser support for nested loops in `src/ate_platform/dsl/parser.py`
3. Generate TypeScript types from the canonical schema:
   - Create `scripts/generate_dsl_types.py` that reads `shared/dsl.py` dataclasses and emits TypeScript interfaces to `frontend/src/types/dsl.ts`
   - Add `npm run generate:types` script to `frontend/package.json`
4. Update `frontend/src/composables/useSerializer.ts` to use generated types
5. Remove hand-maintained `YamlStep`/`YamlSequence` interfaces from `useSerializer.ts`

**Files changed**:
- `src/shared/dsl.py` — add YamlLoop, LoopType, ExecutionMode, StepType; update YamlStep, YamlPlan
- `src/shared/types.py` — add ExecutionMode, StepType, LoopContainer, ParallelGroup
- `src/shared/events.py` — add STEP_STARTED, STEP_COMPLETED, LOOP_ITERATION_STARTED, LOOP_ITERATION_COMPLETED, EXECUTION_STARTED, EXECUTION_COMPLETED
- `src/ate_platform/dsl/parser.py` — parse nested loop structures
- `scripts/generate_dsl_types.py` — NEW: type generator
- `frontend/src/types/dsl.ts` — NEW: generated TypeScript types
- `frontend/src/composables/useSerializer.ts` — use generated types, add loop serialization
- `frontend/package.json` — add generate:types script

**QA**:
```bash
# Backend: parse YAML with loop, round-trip
python -c "
from src.ate_platform.dsl.parser import YamlParser
p = YamlParser()
plan = p.parse('tests/fixtures/loop_plan.yaml')
assert any(hasattr(s, 'loop_type') for s in plan.steps), 'Loop not parsed'
from src.shared.dsl import yaml_dump
yaml_str = yaml_dump(plan)
plan2 = p.parse_string(yaml_str)
assert len(plan2.steps) == len(plan.steps), 'Round-trip failed'
"

# Frontend: generated types exist and match
cd frontend && npx tsc --noEmit src/types/dsl.ts

# Type generation script works
python scripts/generate_dsl_types.py && git diff --exit-code frontend/src/types/dsl.ts
```

---

### Task 0.2: Fix EventBus + Add Execution Context

**Why**: EventBus event schemas are inconsistent (3 different payloads for STEP_STATUS_CHANGED). VARIABLE_CHANGED and RESOURCE_RELEASED are defined but never fired. No execution context (run_id) exists — SSE events can't be filtered. `_publish_status()` uses `asyncio.run()` fallback that creates a new event loop and silently loses events.

**What**:
1. Normalize event schemas — define strict `EventData` dataclasses for each EventType:
   ```python
   @dataclass
   class StepStatusChangedData:
       step_id: str
       status: str
       old_status: str | None = None
       run_id: str | None = None

   @dataclass
   class StepStartedData:
       step_id: str
       run_id: str
       script_path: str
       timestamp: str

   @dataclass
   class StepCompletedData:
       step_id: str
       run_id: str
       status: str
       outputs: dict[str, Any] | None = None
       error: str | None = None
       duration_ms: float | None = None
   ```
2. Add `run_id` to all event data classes
3. Add new EventTypes: `STEP_STARTED`, `STEP_COMPLETED`, `LOOP_ITERATION_STARTED`, `LOOP_ITERATION_COMPLETED`, `EXECUTION_STARTED`, `EXECUTION_COMPLETED`
4. Fix `_publish_status()` in `ProcessExecutor` — replace `asyncio.run()` fallback with thread-safe queue:
   ```python
   self._event_queue: asyncio.Queue | None = None

   def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
       self._event_queue = asyncio.Queue()
       loop.call_soon(self._drain_event_queue, loop)

   def _publish_status(self, step_id, status, run_id=None):
       if self._event_queue is not None:
           self._event_queue.put_nowait((step_id, status, run_id))
       elif self._event_bus is not None:
           try:
               loop = asyncio.get_running_loop()
               loop.create_task(self._event_bus.publish(...))
           except RuntimeError:
               logger.warning(f"Event lost: {step_id} → {status} (no event loop)")
   ```
5. Fire `VARIABLE_CHANGED` from `VariableSpace.set()` and `RESOURCE_RELEASED` from `ResourceManager.release()`
6. Add `ExecutionContext` dataclass with `run_id`, `sequence_id`, `started_at`, `status`

**Files changed**:
- `src/shared/events.py` — add typed event data classes, new EventTypes
- `src/shared/types.py` — add ExecutionContext
- `src/ate_platform/executor/process_executor.py` — fix _publish_status, add set_event_loop
- `src/ate_platform/scheduler/variable_space.py` — fire VARIABLE_CHANGED on set()
- `src/ate_platform/scheduler/resource_manager.py` — fire RESOURCE_RELEASED on release()
- `src/ate_platform/scheduler/scanner_scheduler.py` — use normalized event schemas, add run_id
- `src/ate_platform/scheduler/step_registry.py` — use normalized event schemas

**QA**:
```bash
# Event schemas are consistent
python -c "
from src.shared.events import StepStatusChangedData, StepStartedData
d = StepStatusChangedData(step_id='s1', status='RUNNING', run_id='r1')
assert d.run_id == 'r1', 'run_id missing'
"

# _publish_status never uses asyncio.run()
grep -r 'asyncio.run' src/ate_platform/executor/process_executor.py
# Expected: no matches (or only in comments)

# VARIABLE_CHANGED is actually fired
python -c "
import asyncio
from src.ate_platform.scheduler.event_bus import EventBus
from src.ate_platform.scheduler.variable_space import VariableSpace
from src.shared.events import EventType
bus = EventBus()
vs = VariableSpace(event_bus=bus)
received = []
bus.subscribe(EventType.VARIABLE_CHANGED, lambda e: received.append(e))
asyncio.run(bus.start())
vs.set('scope.x', 42)
assert len(received) > 0, 'VARIABLE_CHANGED not fired'
asyncio.run(bus.stop())
"

# RESOURCE_RELEASED is actually fired
python -c "
import asyncio
from src.ate_platform.scheduler.event_bus import EventBus
from src.ate_platform.scheduler.resource_manager import ResourceManager
from src.shared.events import EventType
bus = EventBus()
rm = ResourceManager(event_bus=bus)
received = []
bus.subscribe(EventType.RESOURCE_RELEASED, lambda e: received.append(e))
asyncio.run(bus.start())
rm.acquire('DUT1', 'owner1')
rm.release('DUT1', 'owner1')
assert len(received) > 0, 'RESOURCE_RELEASED not fired'
asyncio.run(bus.stop())
"
```

---

### Task 0.3: Refactor ProcessExecutor to Async

**Why**: `execute()` is synchronous-blocking (`pool.apply_async()` then `async_result.get()`). Cannot dispatch parallel loop iterations. Must support concurrent step execution for parallel mode.

**What**:
1. Add `execute_async()` method that returns `StepResult` via `asyncio.to_thread`:
   ```python
   async def execute_async(self, script_path, params, step_id=None, timeout=None, run_id=None) -> StepResult:
       return await asyncio.to_thread(self.execute, script_path, params, step_id, timeout)
   ```
2. Add `execute_batch()` for parallel loop iterations:
   ```python
   async def execute_batch(self, tasks: list[ExecuteTask], max_concurrency: int = 4) -> list[StepResult]:
       sem = asyncio.Semaphore(max_concurrency)
       async def _run_with_sem(task):
           async with sem:
               return await self.execute_async(task.script_path, task.params, task.step_id, task.timeout, task.run_id)
       return await asyncio.gather(*[_run_with_sem(t) for t in tasks])
   ```
3. Switch from `multiprocessing.Pool` to `concurrent.futures.ThreadPoolExecutor` for I/O-bound scripts (threading+asyncio model, GIL acceptable for I/O-bound work)
4. Keep `multiprocessing.Pool` as optional for CPU-bound scripts (configurable per-step)
5. Add `run_id` parameter to `execute()`, `execute_async()`, `execute_batch()`
6. Add `ExecutionContext` tracking to executor

**Files changed**:
- `src/ate_platform/executor/process_executor.py` — add execute_async, execute_batch, switch default to ThreadPoolExecutor
- `src/shared/types.py` — add ExecuteTask dataclass

**QA**:
```bash
# execute_async is non-blocking
python -c "
import asyncio
from src.ate_platform.executor.process_executor import ProcessExecutor
ex = ProcessExecutor(max_workers=2)
async def test():
    future = asyncio.create_task(ex.execute_async('tests/fixtures/pass_script.py', {}))
    await asyncio.sleep(0.01)
    assert not future.done() or future.result().status.value in ('PASSED', 'FAILED', 'ERROR')
    result = await future
    assert result is not None
asyncio.run(test())
"

# execute_batch runs concurrently
python -c "
import asyncio, time
from src.ate_platform.executor.process_executor import ProcessExecutor
ex = ProcessExecutor(max_workers=4)
async def test():
    tasks = [
        {'script_path': 'tests/fixtures/pass_script.py', 'params': {}, 'step_id': f's{i}'}
        for i in range(3)
    ]
    results = await ex.execute_batch(tasks, max_concurrency=3)
    assert len(results) == 3, f'Expected 3 results, got {len(results)}'
asyncio.run(test())
"
```

---

### Task 0.4: Add SSE Bridge

**Why**: No real-time push endpoint exists. The NATS→SSE bridge is the core of the real-time status feature.

**What**:
1. Create `src/ate_cloud/api/v1/executions.py` with SSE endpoint:
   ```python
   from sse_starlette.sse import EventSourceResponse

   @router.get("/executions/{run_id}/events", response_class=EventSourceResponse)
   async def stream_execution_events(run_id: str, request: Request):
       queue = get_or_create_queue(run_id)
       async def event_generator():
           last_id = request.headers.get("Last-Event-ID")
           if last_id:
               async for event in replay_from_jetstream(run_id, last_id):
                   yield ServerSentEvent(data=event.json(), event=event.type, id=event.id)
           while True:
               event = await queue.get()
               yield ServerSentEvent(data=event.json(), event=event.type, id=event.id)
       return EventSourceResponse(event_generator())
   ```
2. Create `src/ate_cloud/nats/sse_bridge.py` — bridges NATS subscriber to asyncio.Queue per run_id
3. Add execution CRUD endpoints:
   - `POST /api/v1/executions` — start execution (creates run_id, dispatches to executor)
   - `GET /api/v1/executions/{run_id}` — get execution status
   - `POST /api/v1/executions/{run_id}/abort` — abort execution
4. Create `src/ate_cloud/models/execution.py` — SQLAlchemy model for execution records
5. Create `src/ate_cloud/schemas/execution.py` — Pydantic schemas
6. Register execution router in `src/ate_cloud/api/v1/router.py`
7. Add `sse-starlette` to backend dependencies

**Files changed**:
- `src/ate_cloud/api/v1/executions.py` — NEW: execution CRUD + SSE endpoint
- `src/ate_cloud/nats/sse_bridge.py` — NEW: NATS→SSE bridge
- `src/ate_cloud/models/execution.py` — NEW: Execution SQLAlchemy model
- `src/ate_cloud/schemas/execution.py` — NEW: Execution Pydantic schemas
- `src/ate_cloud/api/v1/router.py` — register execution router
- `src/ate_cloud/main.py` — ensure NATS subscriber starts on boot
- `pyproject.toml` or `requirements.txt` — add sse-starlette

**QA**:
```bash
# SSE endpoint returns events
curl -N http://localhost:8000/api/v1/executions/test-run-001/events &
curl -X POST http://localhost:8000/api/v1/executions -H 'Content-Type: application/json' -d '{"sequence_id": "..."}'
# Should see SSE data lines with STEP_STARTED, STEP_COMPLETED events

# Last-Event-ID resumption
curl -N -H "Last-Event-ID: step-003" http://localhost:8000/api/v1/executions/test-run-001/events
# Should replay events after step-003

# Execution CRUD
curl -X POST http://localhost:8000/api/v1/executions -d '{"sequence_id":"seq-1"}' | python -m json.tool
curl http://localhost:8000/api/v1/executions/run-001 | python -m json.tool
curl -X POST http://localhost:8000/api/v1/executions/run-001/abort
```

---

## Phase 1: Features (2 parallel tracks)

### Track A: Loop Container Nodes (Tasks 1.1–1.5)

#### Task 1.1: Loop Container X6 Shape + Data Model

**What**:
1. Add `LoopContainerData` interface to `frontend/src/models/nodes/types.ts`:
   ```typescript
   export interface LoopContainerData {
     loopId: string
     loopType: 'for' | 'while' | 'foreach'
     condition: string
     iterationVar?: string
     collectionExpr?: string
     count?: number
     executionMode: 'serial' | 'parallel'
     maxConcurrency: number
     status?: 'idle' | 'running' | 'passed' | 'failed' | 'error'
   }
   ```
2. Register `loop-container-node` shape in `frontend/src/main.ts`:
   - Larger dimensions (300x200), dashed border, dashed border, distinct fill color
   - Port groups: input (left), output (right), loop-back (bottom)
   - Collapsed/expanded visual states
3. Update `NodeData` union: `ScriptStepData | VariableData | LoopContainerData`
4. Add type guard `isLoopContainerData()`
5. Add factory `createDefaultLoopContainerData()`

**Files changed**:
- `frontend/src/models/nodes/types.ts` — add LoopContainerData, update NodeData union
- `frontend/src/main.ts` — register loop-container-node shape

**QA**:
```bash
cd frontend
npx vue-tsc --noEmit
```

#### Task 1.2: Scoped Cycle Detection + Loop Back-Edges

**What**:
1. Refactor `useDependencyCheck.ts` — replace `wouldCreateCycle()` with scoped version:
   ```typescript
   function wouldCreateCycle(graph: Graph, source: Cell, target: Cell, containerId?: string): boolean {
     if (containerId && isWithinContainer(graph, source, target, containerId)) {
       return false // Back-edges within loop are OK
     }
     return dfsReachability(graph, target, source)
   }
   ```
2. Update `GraphContainer.vue` `validateConnection` callback to pass container context
3. Set `allowLoop: true` on graph config (currently `false`)
4. Add loop-back port group to loop-container-node (bottom port for back-edges)

**Files changed**:
- `frontend/src/composables/useDependencyCheck.ts` — scoped cycle detection
- `frontend/src/views/SequenceEditor/components/GraphContainer.vue` — update validateConnection, allowLoop=true

**QA**:
```bash
# Back-edge within loop container is allowed
# Cross-container cycle is blocked
```

#### Task 1.3: Embedded Sub-Graph + Breadcrumb Navigation

**What**:
1. Create `SubGraphContainer.vue` — nested X6 Graph instance for loop body editing
2. Create `BreadcrumbNav.vue` — breadcrumb navigation: `主图 > 循环容器名`
3. Modify `GraphContainer.vue` — double-click on loop-container-node → switch to SubGraphContainer
4. Node drop into loop container — detect container under cursor, set parent relationship

**Files changed**:
- `frontend/src/views/SequenceEditor/components/SubGraphContainer.vue` — NEW
- `frontend/src/views/SequenceEditor/components/BreadcrumbNav.vue` — NEW
- `frontend/src/views/SequenceEditor/components/GraphContainer.vue` — add double-click handler, view switching
- `frontend/src/views/SequenceEditor/index.vue` — integrate breadcrumb, view switching

**QA**:
```bash
# Double-click loop container enters sub-graph
# Breadcrumb shows correct path
# Click breadcrumb "主图" returns to main graph
# Dropping node inside loop container sets parent relationship
```

#### Task 1.4: Loop Container YAML Serialization

**What**:
1. Extend `useSerializer.ts` `graphToYaml()` — detect loop containers, recursively serialize children into nested `YamlLoop.steps`
2. Extend `yamlToGraphData()` — parse `YamlLoop` objects, create loop-container-node with children, set parent/child, create loop-back edges
3. Fix `topologicalSort` — handle nested structures, don't silently append cycle-orphans
4. Add `loop_plan.yaml` test fixture

**Files changed**:
- `frontend/src/composables/useSerializer.ts` — add loop serialization/deserialization
- `tests/fixtures/loop_plan.yaml` — NEW: test fixture with loop

**QA**:
```bash
# YAML round-trip preserves loop structure
# Load loop YAML → graph → save → reload → same structure
# Loop back-edges preserved after round-trip
```

#### Task 1.5: PropertyPanel Loop Container Support

**What**:
1. Add loop container section to `PropertyPanel.vue`:
   - Loop type selector (for/while/foreach)
   - Condition/expression input
   - Iteration variable, collection expression, count inputs
   - Execution mode toggle (serial/parallel)
   - Max concurrency slider (parallel mode)
2. Refactor PropertyPanel type dispatch — replace `isScriptStep`/`isVariable` binary with type-switch
3. Add "Loop" category to `StepLibraryPanel.vue` with draggable loop templates

**Files changed**:
- `frontend/src/views/SequenceEditor/components/PropertyPanel.vue` — add loop container section, refactor type dispatch
- `frontend/src/views/SequenceEditor/components/StepLibraryPanel.vue` — add Loop category

**QA**:
```bash
# Select loop container → PropertyPanel shows loop fields
# Change loop type → condition fields update
# Toggle execution mode → max concurrency appears/disappears
# Drag loop template from StepLibraryPanel → creates loop container on canvas
```

---

### Track B: Monaco Editor + Script Content API (Tasks 1.6–1.9)

#### Task 1.6: Script Content API (Backend)

**What**:
1. Add `GET /api/v1/scripts/{id}/content` — read script file content at `script_path`
2. Add `PUT /api/v1/scripts/{id}/content` — write script file content + Git commit
3. Add `GET /api/v1/scripts/{id}/versions` — list Git commit history
4. Add `GET /api/v1/scripts/{id}/versions/{hash}` — get specific version content
5. Create `src/ate_cloud/services/script_versioning.py` — GitPython-based versioning service
6. Add Pydantic schemas: `ScriptContentResponse`, `ScriptContentUpdate`, `ScriptVersionInfo`, `ScriptVersionListResponse`
7. Add `gitpython` to backend dependencies
8. Configure scripts root path via environment variable `SCRIPTS_ROOT_DIR`

**Files changed**:
- `src/ate_cloud/api/v1/scripts.py` — add content/versions endpoints
- `src/ate_cloud/services/script_versioning.py` — NEW: GitPython versioning service
- `src/ate_cloud/schemas/script.py` — add content/version schemas
- `src/ate_cloud/main.py` — configure scripts root
- `pyproject.toml` — add gitpython dependency

**QA**:
```bash
# Read script content
curl http://localhost:8000/api/v1/scripts/{id}/content
# Expected: { "content": "...", "version": "abc123", "last_modified": "..." }

# Write script content (creates Git commit)
curl -X PUT http://localhost:8000/api/v1/scripts/{id}/content \
  -H 'Content-Type: application/json' \
  -d '{"content": "print(\"hello\")", "commit_message": "Update script"}'
# Expected: 200 OK, Git commit created

# List versions
curl http://localhost:8000/api/v1/scripts/{id}/versions
# Expected: list of { "hash", "message", "author", "timestamp" }

# Read specific version
curl http://localhost:8000/api/v1/scripts/{id}/versions/{hash}
# Expected: Python source at that commit
```

#### Task 1.7: Frontend Script Content API Client

**What**:
1. Add to `frontend/src/api/scripts.ts`:
   - `fetchScriptContent(id)` → `GET /scripts/{id}/content`
   - `updateScriptContent(id, content, commitMessage?)` → `PUT /scripts/{id}/content`
   - `fetchScriptVersions(id)` → `GET /scripts/{id}/versions`
   - `fetchScriptVersionContent(id, hash)` → `GET /scripts/{id}/versions/{hash}`
2. Add TypeScript interfaces: `ScriptContentResponse`, `ScriptContentUpdate`, `ScriptVersionInfo`
3. Add `script_path` to frontend `Script` interface (currently missing)

**Files changed**:
- `frontend/src/api/scripts.ts` — add content/versions API functions + types

**QA**:
```bash
cd frontend && npx vue-tsc --noEmit
```

#### Task 1.8: Monaco Editor Component

**What**:
1. Install `monaco-editor` + `vite-plugin-monaco-editor`
2. Configure Vite plugin in `vite.config.ts`
3. Create `frontend/src/components/MonacoEditor.vue`:
   - Props: `modelValue`, `language` (default: 'python'), `readOnly`, `theme`
   - Emits: `update:modelValue`, `save`
   - v-model compatible, dark/light theme sync, Ctrl+S save
4. Create `frontend/src/components/ScriptEditorDialog.vue`:
   - Full-screen dialog with Monaco editor
   - Version selector dropdown (Git history)
   - Save button with commit message input
   - Diff view (current vs selected version)

**Files changed**:
- `frontend/package.json` — add monaco-editor, vite-plugin-monaco-editor
- `frontend/vite.config.ts` — configure Monaco plugin
- `frontend/src/components/MonacoEditor.vue` — NEW: reusable Monaco wrapper
- `frontend/src/components/ScriptEditorDialog.vue` — NEW: full-screen script editor dialog

**QA**:
```bash
cd frontend
npm run build  # Build succeeds with Monaco
npx vue-tsc --noEmit  # No TypeScript errors
```

#### Task 1.9: Monaco Integration into PropertyPanel

**What**:
1. Add inline code preview to PropertyPanel when script step is selected:
   - Show first 10 lines of script content in a read-only mini Monaco editor
   - "Edit Script" button opens `ScriptEditorDialog`
2. Wire `ScriptEditorDialog` to script content API:
   - Load content on open, save with commit message, version history dropdown
3. Add "Edit Script" context menu item on script step nodes

**Files changed**:
- `frontend/src/views/SequenceEditor/components/PropertyPanel.vue` — add inline code preview + edit button
- `frontend/src/views/SequenceEditor/components/GraphContainer.vue` — add context menu item

**QA**:
```bash
# Select script step → PropertyPanel shows code preview
# Click "Edit Script" → dialog opens with full Monaco editor
# Edit + save → Git commit created, content updated
# Version dropdown → shows history, selecting version loads content
```

---

## Phase 2: Frontend Real-time Status (Task 2.1)

### Task 2.1: SSE Client + Node Status Updates

**What**:
1. Create `frontend/src/composables/useExecutionStatus.ts`:
   ```typescript
   // NOTE: @vueuse/core useEventSource signature is useEventSource(url, options?)
   // Event type filtering is done manually from the data ref, not as a constructor arg
   export function useExecutionStatus(runId: Ref<string>) {
     const { data, status, error } = useEventSource(
       computed(() => `/api/v1/executions/${runId.value}/events`),
       { autoReconnect: { retries: 10, delay: 2000 } }
     )
     const stepStatuses = reactive<Record<string, StepStatus>>({})
     watch(data, (raw) => {
       if (!raw) return
       const event = JSON.parse(raw)
       // Filter for relevant event types
       const relevantTypes = ['STEP_STARTED', 'STEP_COMPLETED', 'LOOP_ITERATION_STARTED', 'LOOP_ITERATION_COMPLETED']
       if (event.type && relevantTypes.includes(event.type) && event.step_id) {
         stepStatuses[event.step_id] = event.status
       }
     })
     return { stepStatuses, status, error }
   }
   ```
2. Integrate into `GraphContainer.vue` — update node visual status on events
3. Add execution control toolbar: Run/Abort buttons, progress indicator
4. Add `@vueuse/core` dependency (for `useEventSource`)

**Files changed**:
- `frontend/src/composables/useExecutionStatus.ts` — NEW
- `frontend/src/views/SequenceEditor/components/GraphContainer.vue` — integrate status updates
- `frontend/src/views/SequenceEditor/components/Toolbar.vue` — add Run/Abort buttons
- `frontend/package.json` — add @vueuse/core

**QA**:
```bash
# Start execution from toolbar → nodes update status in real-time
# Abort button stops execution
# Disconnect/reconnect resumes events (Last-Event-ID)
```

---

## Phase 3: Backend Loop Execution (Task 3.1)

### Task 3.1: Loop Container Execution Engine

**What**:
1. Create `src/ate_platform/executor/loop_executor.py`:
   - `LoopExecutor` class with `execute_loop()` method
   - Support for/while/foreach loop types
   - Serial mode: iterate sequentially
   - Parallel mode: use `execute_batch()` with semaphore
   - Publish LOOP_ITERATION_STARTED/COMPLETED events
2. Integrate with `ScannerScheduler` — add loop-aware step scanning
3. Add `LoopResult`, `LoopIterationResult` to `src/shared/types.py`
4. Add loop iteration scopes to `VariableSpace` — `loop.<loop_id>.<iteration>.<key>`

**Files changed**:
- `src/ate_platform/executor/loop_executor.py` — NEW
- `src/ate_platform/scheduler/scanner_scheduler.py` — loop-aware scanning
- `src/shared/types.py` — add LoopResult, LoopIterationResult

**QA**:
```bash
# Serial loop execution
python -c "
import asyncio
from src.ate_platform.executor.loop_executor import LoopExecutor
from src.shared.dsl import YamlLoop, YamlStep
loop = YamlLoop(id='loop1', loop_type='for', count=3, execution_mode='serial',
                steps=[YamlStep(id='s1', script='pass_script.py')])
ex = LoopExecutor()
result = asyncio.run(ex.execute_loop(loop, context))
assert len(result.iteration_results) == 3
"

# Parallel loop execution
python -c "
import asyncio
from src.ate_platform.executor.loop_executor import LoopExecutor
loop = YamlLoop(id='loop1', loop_type='for', count=3, execution_mode='parallel', max_concurrency=2,
                steps=[YamlStep(id='s1', script='pass_script.py')])
ex = LoopExecutor()
result = asyncio.run(ex.execute_loop(loop, context))
assert len(result.iteration_results) == 3
"
```

---

## Dependency Graph

```
Phase 0 (sequential — each depends on previous):
  0.1 Unify DSL ──→ 0.2 Fix EventBus ──→ 0.3 Async Executor ──→ 0.4 SSE Bridge

Phase 1 (parallel tracks after Phase 0):
  Track A: 1.1 → 1.2 → 1.3 → 1.4 → 1.5 (loop containers)
  Track B: 1.6 → 1.7 → 1.8 → 1.9 (Monaco editor)

Phase 2 (depends on 0.4 + Track A):
  2.1 SSE Client + Node Status

Phase 3 (depends on 0.3 + Track A):
  3.1 Loop Execution Engine
```

## Cross-Platform Considerations

- **Threading model**: threading+asyncio (no spawn/fork differences across platforms)
- **PyVISA**: default to pyvisa-py (pure Python), NI-VISA optional
- **Signal handling**: threading.Event (cross-platform), no POSIX dependency
- **File paths**: enforce pathlib.Path everywhere
- **GitPython**: shallow script directory structure to avoid Windows path-length issues
- **DB drivers**: asyncpg (ARM64 wheel check) + aiomysql (pure Python) + aiosqlite

## Risk Mitigations

| Risk | Mitigation |
|---|---|
| @antv/x6-vue-shape version incompatibility | Verify X6 v3.x compatibility before use; use plain SVG shapes if needed |
| VariableSpace not cross-process safe | Use threading+asyncio (not multiprocessing) for parallel iterations |
| GitPython Windows path issues | Shallow script directory structure; test on Windows early |
| SSE proxy buffering | FastAPI sets X-Accel-Buffering: no automatically |
| topologicalSort cycle-orphan corruption | Rewrite for nested structures in Task 1.4 |
