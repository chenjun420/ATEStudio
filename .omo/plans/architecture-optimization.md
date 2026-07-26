# architecture-optimization - Work Plan

## TL;DR (For humans)

**What you'll get:** A faster, safer, and more extensible ATE test platform. The scheduler responds instantly instead of polling every 100ms. Instrument drivers are split into clean layers so new instruments are easy to add. The frontend handles 1000+ status updates per second smoothly. Tests can skip themselves based on earlier results, cutting wasted test time. And the platform detects stalls, deadlocks, and overload before they become outages.

**Why this approach:** The current code works but has five specific problems that compound as scale grows — the 100ms polling delay, the tangled instrument driver code, the unbuffered frontend updates, the incomplete loop editor support, and the invisible failure modes (stalled scheduler, full upload queue). Each of these is a low-risk, high-value fix grounded in published research from 2023-2026. The changes are layered in waves so each wave can be tested and deployed independently.

**What it will NOT do:**
- Add OPC UA factory integration or Docker deployment
- Train machine learning models for test optimization
- Change SSE to WebSocket or another protocol
- Use multiprocessing for variable sharing (stays threading)

**Effort:** Large — 16 tasks across 5 sequential waves, ~3-4 tasks per wave
**Risk:** Medium — core scheduler refactoring (Tasks 1-2) is the highest-risk area; mitigated by keeping the scan loop as a 3-tick safety net and comprehensive existing test coverage
**Decisions I made for you:** I treated this as open-ended ("suggest optimizations across the board") and chose industry best-practice defaults from 20+ cited sources. Key defaults you can veto: reactive dispatch fully replaces polling (not coexists), dagre not elkjs for layout, rule-based skip before ML, HAL/MAL two layers (FAL deferred), SSE stays as-is, threading not multiprocessing for VariableSpace. If you had a specific outcome in mind beyond these dimensions, say so and I will switch to asking instead of defaulting.

Your next move: approve the plan to proceed to high-accuracy dual review (Momus + Oracle). Full execution detail follows below.

---

> TL;DR (machine): Large effort, Medium risk — 16 tasks across 5 waves: reactive scheduler, HAL/MAL drivers, TEMS A4 events, SSE hardening, adaptive skip, frontend batching/layout/serialization, WatchDog, pool guard, upload pruning, RAG failure indexing.

## Scope
### Must have
1. EventBus hardening: subscriber isolation (failing subscriber doesn't block others) + publish_sync event queuing
2. Reactive dispatch: wire existing event handlers to trigger dispatch; deprecate 100ms polling to 5s watchdog
3. StepExecutor Protocol: extract from ScannerScheduler into interface with Process/Thread implementations
4. HAL/MAL driver separation: split InstrumentDriver into BaseDriver (SCPI) + BaseAbstraction (semantic)
5. Pydantic capability models: instrument Capability models with auto-generated Mock via factory
6. TEMS A4-aligned event schema: reorganize `shared/events.py` into event/measurement/alarm categories
7. SSE hardening: fix replay pagination bug, reduce heartbeat to 15s, add local-mode heartbeat, multi-client support
8. DSL `skip_if` precondition: rule-based adaptive skipping in YamlStep/YamlLoop
9. RAG failure case indexing: Qdrant index for failed test sequences alongside successes
10. Frontend state batching: 50ms dedup window before X6 setData() updates
11. Loop container serialization: complete YAML ↔ Graph conversion for FOR/WHILE/FOREACH
12. dagre auto-layout: replace fixed grid layout with topological DAG layout
13. Upload queue pruning: max-size + TTL config for SQLiteCache upload_queue
14. Web Worker cycle detection: offload DFS from UI thread for >50 nodes
15. WatchDog health monitor: independent asyncio heartbeat monitor for _scan_loop
16. Pool exhaustion guard: deadlock detection via resource-holder analysis in ProcessExecutor

### Must NOT have (guardrails, anti-slop, scope boundaries)
1. OPC UA Companion Specification integration
2. Docker containers / compose / Dockerfile
3. FAL (Fixture Abstraction Layer) — product-specific, deferred
4. ML model training for adaptive testing — rule-based only in this plan
5. Streamable HTTP or WebSocket migration from SSE
6. elkjs auto-layout (use dagre only)
7. Playwright/Cypress E2E frontend testing (Vitest component tests only)
8. Multiprocessing.Pool as default executor (ThreadPoolExecutor stays default; use_multiprocessing=True unsupported until process-safe VariableSpace adapter built)
9. Distributed multi-station scheduler coordination
10. New database tables or migrations (only code refactoring and new logic)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after + pytest (Python) + Vitest (TypeScript)
- Framework: pytest + pytest-asyncio (backend), Vitest + @vue/test-utils (frontend)
- Evidence: `.omo/evidence/task-<N>-architecture-optimization.<ext>` — JSON log with assertion results, test output, coverage delta
- Each task includes exact QA commands for happy + failure paths
- Backend tests run against SQLite in-memory; frontend tests run with jsdom
- NATS-dependent tests use `nats-py` in-process server or mock
- Existing test suites in `tests/unit/` and `tests/cloud/` must continue passing

## Execution strategy
### Parallel execution waves
> Target 3-5 todos per wave. Each wave completes before the next starts (sequential waves), but todos within a wave are parallelizable unless blocked by explicit dependencies.

**Wave 1 (P0 Critical — scheduler + instrument foundations)**: Tasks 1-5. EventBus hardening, reactive dispatch, StepExecutor extraction, HAL/MAL separation, capability models. These form the core architectural improvements and have no cross-wave dependencies.

**Wave 2 (P0+P1 — streaming + adaptive execution)**: Tasks 6-9. TEMS A4 event schema, SSE hardening, DSL skip_if, state batching. Depends on Wave 1 for the executor and event infrastructure.

**Wave 3 (P1 — frontend completeness)**: Tasks 10-12. Loop serialization, auto-layout, Web Worker offload. Depends on Wave 2 for the event schema (loop events are part of the new schema).

**Wave 4 (P2 — resilience hardening)**: Tasks 13-16. Upload queue pruning, WatchDog, pool exhaustion guard, RAG failure indexing. Depends on Wave 1 for executor structure and Wave 2 for event schema.

**Note on loop-monaco-sse plan**: The active plan `.omo/plans/loop-monaco-sse.md` handles foundational work (unified YAML DSL schema, EventBus normalization, SSE endpoint, LoopExecutor) that this optimization plan builds ON TOP of. Tasks in this plan assume loop-monaco-sse is complete OR fold its remaining work into the relevant tasks. Specifically: Task 10 (loop serialization) subsumes the loop serialization work in loop-monaco-sse; Task 6 (event schema) supersedes the event normalization in loop-monaco-sse Task 0.2.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 (EventBus hardening) | — | 2, 6, 14 | 3, 4, 5 |
| 2 (Reactive dispatch) | 1 (EventBus fixes) | 6, 13, 14, 15 | 3, 4, 5 |
| 3 (StepExecutor) | — | 15 | 1, 2, 4, 5 |
| 4 (HAL/MAL separation) | — | 5 | 1, 2, 3 |
| 5 (Capability models) | 4 | — | 1, 2, 3 |
| 6 (TEMS A4 events) | 1, 2 (executor events), loop-monaco-sse Task 0.1 (unified DSL) | 7, 8, 9, 10 | — |
| 7 (SSE hardening) | 6 (event schema) | — | 8, 9 |
| 8 (DSL skip_if) | 6 (event schema), 2 (dispatch) | — | 7, 9 |
| 9 (State batching) | 6 (event schema) | — | 7, 8 |
| 10 (Loop serialization) | 6 (event schema), loop-monaco-sse Task 0.1 (YamlLoop type) | — | 11, 12 |
| 11 (dagre auto-layout) | — | — | 10, 12 |
| 12 (Web Worker) | — | — | 10, 11 |
| 13 (Upload queue pruning) | 2 (executor) | — | 14, 15, 16 |
| 14 (WatchDog) | 1, 2 (executor structure) | — | 13, 15, 16 |
| 15 (Pool exhaustion guard) | 2, 3 (executor structure) | — | 13, 14, 16 |
| 16 (RAG failure indexing) | — | — | 13, 14, 15 |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->

### Wave 1: Scheduler + Instrument Foundations (P0)

- [x] 1. Harden EventBus: prevent silent subscriber failure and fix publish_sync event loss
  **What to do**: The EventBus is the critical path for reactive dispatch. Two bugs must be fixed before reactive dispatch can be trusted.
  - **Bug 1 — Silent subscriber failures**: In `_dispatch_event()` (L217-234), exceptions from subscriber callbacks are caught with `except Exception: continue`, which means a failing subscriber silently breaks delivery to subsequent subscribers in the same dispatch cycle. Fix: catch per-subscriber, log the exception with subscriber name and stack trace, then continue to next subscriber.
  - **Bug 2 — publish_sync drops events**: `publish_sync()` (L90-129) calls `asyncio.run_coroutine_threadsafe()` which silently returns without queuing if no event loop is available (L124: "No loop available — event is lost"). Under reactive dispatch, a dropped `VARIABLE_CHANGED` from `VariableSpace.set()` means dependent steps never trigger. Fix: add a `_pending_queue: list` for events published while loop is unavailable; on `set_event_loop()`, drain the pending queue.
  - Add `EventBus.stats` property returning `{ published, delivered, dropped, pending }` for monitoring.
  - Add tests that verify: (1) subscriber exception doesn't break other subscribers, (2) `publish_sync` without loop queues events for later delivery.
  **Must NOT do**: Change the EventBus public API (publish/subscribe signatures remain the same); add persistence or at-least-once delivery guarantees (watchdog scan handles recovery).
  **Parallelization**: Wave 1 | Blocked by: — | Blocks: 2, 6, 14
  **References**:
  - `src/ate_platform/scheduler/event_bus.py:237-260` — `_dispatch_event()` with silent exception swallowing at L258-260
  - `src/ate_platform/scheduler/event_bus.py:90-129` — `publish_sync()` with "event is lost" at L124-129
  - `src/ate_platform/scheduler/variable_space.py:194-202` — `VariableSpace.set()` calls `publish_sync()`
  - `tests/unit/scheduler/test_event_bus.py` — existing event bus tests
  **Acceptance criteria**:
  - Subscriber that raises `ValueError` does not prevent subsequent subscribers from receiving the event
  - `publish_sync()` called before `set_event_loop()` queues events; after loop is set, all queued events are delivered
  - `EventBus.stats.dropped == 0` after reactive dispatch runs for 100 events
  - `EventBus.stats.pending` decreases as events are delivered
  - Existing event bus tests pass unchanged
  **QA scenarios**:
  - Happy: `pytest tests/unit/scheduler/test_event_bus.py -v -k "subscriber_exception"` — failing subscriber doesn't block others
  - Happy: `pytest tests/unit/scheduler/test_event_bus.py -v -k "publish_sync_queuing"` — queued events delivered after loop available
  - Failure: 100 rapid `publish_sync` calls without loop → all 100 delivered after `set_event_loop()`, no drops
  - Regression: `pytest tests/unit/scheduler/test_event_bus.py -v` — all existing tests pass
  - Evidence: `.omo/evidence/task-1-architecture-optimization.json`
  **Commit**: Y | `fix(scheduler): harden EventBus subscriber isolation and publish_sync event queuing`

- [x] 2. Complete reactive dispatch by wiring existing event handlers and deprecating _scan_loop polling
  **What to do**: The `ScannerScheduler` already registers event handlers at L161-194 (`on_variable_changed`, `on_step_status_changed`, `on_resource_released`) but they are no-ops that only log. Wire them to trigger actual dispatch.
  - In `on_step_status_changed()`: after logging, call `_evaluate_dependents(step_id)` which runs `_registry.evaluate_readiness()` ONLY for steps whose preconditions include the completed step (use `_dependency_index` built at `compile_plan()` time). For each newly-ready step, call `_dispatch_step()`.
  - In `on_variable_changed()`: same pattern — evaluate all steps whose preconditions reference the changed variable, dispatch ready ones.
  - In `on_resource_released()`: evaluate all steps blocked on the released resource, dispatch ready ones.
  - In `_scan_loop()`: change from a 100ms busy-loop to a 5-second watchdog. After reactive dispatch is wired, the loop's only job is to detect missed events: if `_last_scan_progress` hasn't changed in 5s, call `_emergency_scan()` which does a full `get_ready_steps()` + dispatch (one-shot, not a loop). This catches any event that was dropped or never emitted.
  - Add `_pending_dispatch: set[step_id]` to deduplicate concurrent dispatch calls — if `_dispatch_step()` is called for a step already being dispatched, it's a no-op.
  - Fix `LoopExecutor._execute_iterations_parallel()` race condition: parallel iterations share `VariableSpace` via threads. Before dispatching iterations, snapshot variable values into per-iteration copies to prevent cross-iteration pollution.
  **Must NOT do**: Remove the deadlock detection (it moves to WatchDog in Task 14); change the public API of `ScannerScheduler.start()/stop()`; add new thread pools (reuse existing executor).
  **Parallelization**: Wave 1 | Blocked by: 1 (EventBus fixes) | Blocks: 6, 13, 14, 15
  **References**:
  - `src/ate_platform/scheduler/scanner_scheduler.py:155-194` — `_register_event_handlers()` with no-op callbacks
  - `src/ate_platform/scheduler/scanner_scheduler.py:202-260` — `_scan_loop()` with `_scan_interval = 0.1`
  - `src/ate_platform/scheduler/scanner_scheduler.py:76-92` — `compile_plan()` where `_dependency_index` should be built
  - `src/ate_platform/scheduler/step_registry.py:80-120` — `get_ready_steps()`, `evaluate_readiness()`
  - `src/ate_platform/executor/loop_executor.py:140-180` — `_execute_iterations_parallel()` shared VariableSpace
  - `tests/unit/scheduler/test_scanner_scheduler.py` — existing scheduler tests
  **Acceptance criteria**:
  - `on_step_status_changed()` triggers `_evaluate_dependents(step_id)` and dispatches newly-ready steps
  - `on_variable_changed()` triggers dispatch for steps whose preconditions reference the variable
  - `on_resource_released()` triggers dispatch for resource-blocked steps
  - `_scan_loop` no longer polls at 100ms; instead polls at 5s and only runs `_emergency_scan()` if no progress
  - `_emergency_scan()` catches a step that should have been dispatched but wasn't (simulated by suppressing an event in test)
  - `_pending_dispatch` deduplication prevents double-dispatch of the same step
  - Parallel loop iterations use per-iteration VariableSpace snapshots, not shared mutable space
  - Existing tests in `tests/unit/scheduler/test_scanner_scheduler.py` pass
  - New test: `pytest tests/unit/scheduler/test_scanner_scheduler.py -v -k "reactive_dispatch"` — step dispatched within same event callback tick
  **QA scenarios**:
  - Happy: `pytest tests/unit/scheduler/test_scanner_scheduler.py -v -k "reactive"` — reactive dispatch triggers immediately
  - Happy: `pytest tests/unit/scheduler/test_scanner_scheduler.py -v -k "emergency_scan"` — 5s watchdog catches missed event
  - Failure: Suppress `STEP_COMPLETED` event for step A→B chain, verify B is dispatched by `_emergency_scan()` within 5s
  - Regression: `pytest tests/unit/scheduler/ -v` — all existing scheduler tests pass
  - Evidence: `.omo/evidence/task-2-architecture-optimization.json`
  **Commit**: Y | `refactor(scheduler): wire reactive dispatch via event handlers, deprecate polling to 5s watchdog`

- [x] 3. Extract StepExecutor Protocol with Process/Thread implementations
  **What to do**: Create `StepExecutor` Protocol in new file `src/ate_platform/executor/step_executor.py`.
  - Define `StepExecutor` Protocol: `execute(step: ExecuteTask, context: ExecutionContext) -> StepResult` (sync) and `execute_async(step, context) -> Awaitable[StepResult]` (async).
  - `ProcessStepExecutor`: wraps `ProcessExecutor` with ThreadPoolExecutor; maintains active worker count.
  - `ThreadStepExecutor`: wraps `concurrent.futures.ThreadPoolExecutor` directly; for testing and CPU-light scripts.
  - Refactor `ScannerScheduler.execute_loop_step()` to delegate to `self._step_executor.execute_async()`.
  - Inject executor via constructor: `ScannerScheduler(..., step_executor: StepExecutor = ProcessStepExecutor())`.
  **Must NOT do**: Remove ProcessExecutor entirely (loop execution still uses it); change the Executor public API for loop_executor.py (that's a separate concern).
  **Parallelization**: Wave 1 | Blocked by: — | Blocks: 15
  **References**:
  - `src/ate_platform/scheduler/scanner_scheduler.py:176-184` — `execute_loop_step()` hardcodes ProcessExecutor
  - `src/ate_platform/executor/process_executor.py:30-80` — `ProcessExecutor.execute()` and `execute_async()`
  - `src/shared/types.py:40-60` — `ExecuteTask`, `StepResult`, `ExecutionContext`
  - `tests/unit/executor/test_process_executor.py` — existing process executor tests
  **Acceptance criteria**:
  - `StepExecutor` Protocol defined with `execute()` and `execute_async()` signatures
  - `ProcessStepExecutor` passes existing `test_process_executor.py` tests
  - `ThreadStepExecutor` created with tests for thread-based execution
  - `ScannerScheduler.__init__` accepts `step_executor` parameter with default
  - `execute_loop_step` delegates to `self._step_executor` instead of creating ProcessExecutor inline
  **QA scenarios**:
  - Happy: `pytest tests/unit/executor/test_step_executor.py -v` — new test file for StepExecutor implementations
  - Happy: `pytest tests/unit/scheduler/test_scanner_scheduler.py -v -k "executor_injection"` — scheduler uses injected executor
  - Failure: Inject a failing executor, verify `execute_loop_step` propagates the error with proper logging
  - Regression: `pytest tests/unit/executor/ -v` and `pytest tests/unit/scheduler/ -v` — all existing tests pass
  - Evidence: `.omo/evidence/task-3-architecture-optimization.json`
  **Commit**: Y | `refactor(executor): extract StepExecutor Protocol with Process/Thread implementations`

- [x] 4. Split instrument drivers into HAL (BaseDriver) and MAL (BaseAbstraction) layers
  **What to do**: Separate communication protocol from semantic measurement logic.
  - Rename current `InstrumentDriver` to `BaseDriver` in `src/ate_platform/drivers/base_hal.py` — keeps `connect()`, `disconnect()`, `write()`, `query()`, `read()`, `reset()` (pure SCPI/VISA).
  - Create `BaseAbstraction` in `src/ate_platform/drivers/base_mal.py` — takes a `BaseDriver` via constructor, provides semantic methods that MUST be overridden by concrete abstractions.
  - Refactor DMMDriver: split into `DMMHALDriver` (HAL, extends `BaseDriver`) and `DMMAbstraction` (MAL, extends `BaseAbstraction`, methods: `measure_voltage(range)`, `measure_current(range)`, `measure_resistance(range)`).
  - Refactor PSUDriver: split similarly — `PSUHALDriver` (HAL) and `PSUAbstraction` (MAL, methods: `set_voltage()`, `set_current()`, `enable_output()`, `measure_output()`).
  - Update `DriverRegistry` to register BOTH HAL and MAL; `get_driver()` returns MAL abstraction (not raw HAL). Registry type check must accept both `BaseDriver` and `BaseAbstraction` subclasses (relax `issubclass` check).
  - Mock drivers (Task 4) will implement MAL interface directly.
  **Must NOT do**: Change the SCPI command strings (keep existing instrument compatibility); introduce FAL layer (deferred).
  **Parallelization**: Wave 1 | Blocked by: — | Blocks: 5
  **References**:
  - `src/ate_platform/drivers/base.py:10-60` — `InstrumentDriver` (ABC), `DriverRegistry`
  - `src/ate_platform/drivers/examples/dmm.py:20-100` — DMMDriver (SCPI mixed with semantics)
  - `src/ate_platform/drivers/examples/psu.py:20-95` — PSUDriver (SCPI mixed with semantics)
  - `tests/unit/drivers/test_base.py` — existing driver tests
  - `tests/unit/drivers/test_examples.py` — DMM/PSU tests
  - NI HAL/MAL/FAL pattern (National Instruments, 2024) — library research
  **Acceptance criteria**:
  - `BaseDriver` exists with `write/query/read/connect/disconnect/reset` (VISA/SCPI only)
  - `BaseAbstraction` exists with `self._driver: BaseDriver` and abstract semantic methods
  - `DMMAbstraction.measure_voltage()` calls `self._driver.query("MEAS:VOLT:DC?")` — HAL call via composition, not inheritance
  - `PSUAbstraction.set_voltage(5.0)` calls `self._driver.write("VOLT 5.0")`
  - `DriverRegistry.get_driver("dmm")` returns a `DMMAbstraction` instance
  - Existing `test_examples.py` tests pass after refactor
  **QA scenarios**:
  - Happy: `pytest tests/unit/drivers/test_examples.py -v` — all DMM/PSU tests pass through new abstraction layer
  - Happy: `pytest tests/unit/drivers/test_base.py -v -k "hal_mal"` — new tests verifying HAL/MAL separation
  - Failure: Inject a failing BaseDriver, verify BaseAbstraction propagates the error with instrument context in message
  - Regression: `pytest tests/unit/drivers/ -v` — all driver tests pass
  - Evidence: `.omo/evidence/task-4-architecture-optimization.json`
  **Commit**: Y | `refactor(drivers): split InstrumentDriver into BaseDriver (HAL) and BaseAbstraction (MAL)`

- [x] 5. Add Pydantic instrument capability models and auto-mock factory
  **What to do**: Define capability models for each instrument and a MockDriverFactory that generates mock implementations.
  - New file `src/ate_platform/drivers/capabilities.py`: `DMMCapabilities(BaseModel)` with `channels: int = 1`, `max_voltage: float`, `max_current: float`, `can_measure_resistance: bool = True`, `can_measure_current: bool = True`, `resolution_digits: float = 6.5`.
  - `PSUCapabilities(BaseModel)`: `channels: int`, `max_voltage: float`, `max_current: float`, `has_remote_sense: bool = False`.
  - `BaseAbstraction` gains `capabilities: ClassVar[Type[BaseModel]]` and `get_capabilities() -> BaseModel` that queries instrument (`*IDN?` for basic, then model-specific capability query).
  - New file `src/ate_platform/drivers/mock_factory.py`: `MockDriverFactory` with `create_mock(abstraction_cls: Type[BaseAbstraction]) -> BaseAbstraction` — auto-generates a mock that returns sensible defaults (voltage=0.0, current=0.0) from capabilities fields.
  - Remove manual `MockDMMDriver` and `MockPSUDriver` classes.
  **Must NOT do**: Auto-detect real vs mock at driver registration time — the caller decides which to use; add network-based capability discovery (SCPI only).
  **Parallelization**: Wave 1 | Blocked by: 4 | Blocks: —
  **References**:
  - `src/ate_platform/drivers/examples/dmm.py:100-130` — manual MockDMMDriver to be replaced
  - `src/ate_platform/drivers/examples/psu.py:95-120` — manual MockPSUDriver to be replaced
  - Instrumation 2026 Python modern HAL blog — capability models pattern
  - `tests/unit/drivers/test_examples.py:50-80` — existing mock driver tests (adapt, not break)
  **Acceptance criteria**:
  - `DMMCapabilities` and `PSUCapabilities` Pydantic models defined with all fields
  - `MockDriverFactory.create_mock(DMMAbstraction)` returns a `DMMAbstraction` with mock BaseDriver that responds to SCPI queries
  - `mock_dmm.measure_voltage()` returns 0.0 (default) or a configurable value via `mock_dmm._mock_values` dict
  - Existing mock-based tests in `test_examples.py` pass without manual MockDMMDriver/PSUDriver
  - New test: verify capability models validate correctly (e.g., `DMMCapabilities(channels=-1)` raises ValidationError)
  **QA scenarios**:
  - Happy: `pytest tests/unit/drivers/test_capabilities.py -v` — new tests for capability models
  - Happy: `pytest tests/unit/drivers/test_mock_factory.py -v` — auto-mock generates correct mock behavior
  - Happy: `pytest tests/unit/drivers/test_examples.py -v` — existing mock tests pass via factory
  - Failure: Request capabilities from a driver that doesn't support capability query — expect clear NotImplementedError
  - Evidence: `.omo/evidence/task-5-architecture-optimization.json`
  **Commit**: Y | `feat(drivers): add Pydantic capability models and auto-mock factory`

### Wave 2: Streaming + Adaptive Execution (P0+P1)

- [x] 6. Reorganize event schema to TEMS A4 categories (event/measurement/alarm)
  **What to do**: Refactor `src/shared/events.py` to classify all events into three TEMS A4-aligned categories with standardized fields.
  - Define `EventCategory` enum: `EVENT`, `MEASUREMENT`, `ALARM`.
  - Add `category: EventCategory` field to `Event` dataclass.
  - `EVENT` category: step lifecycle events — `STEP_STARTED`, `STEP_COMPLETED`, `STEP_FAILED`, `STEP_SKIPPED`, `LOOP_ITERATION_STARTED`, `LOOP_ITERATION_COMPLETED`, `EXECUTION_STARTED`, `EXECUTION_COMPLETED`, `EXECUTION_PAUSED`.
  - `MEASUREMENT` category: `MEASUREMENT_RECORDED` (renamed from `VARIABLE_CHANGED`) — adds `timestamp: float`, `unit: str | None`, `instrument_id: str | None` to the data payload.
  - **Backward compatibility**: Keep `VARIABLE_CHANGED` as a deprecated alias in `EventType` that maps to the same value as `MEASUREMENT_RECORDED` (e.g., `VARIABLE_CHANGED = MEASUREMENT_RECORDED = "measurement_recorded"`). This means existing NATS consumers that match on the old enum string will get the old value until they update. Log a deprecation warning when `VARIABLE_CHANGED` is referenced. Add a `DeprecationWarning` test.
  - `ALARM` category: `STEP_TIMEOUT`, `CONDITION_TIMEOUT`, `RESOURCE_TIMEOUT`, `DEADLOCK_DETECTED`, `WORKER_EXHAUSTED` — adds `severity: Literal["warning", "critical"]`, `recoverable: bool`.
  - Update all event publishers in scheduler (`event_bus.py`, `scanner_scheduler.py`, `variable_space.py`, `resource_manager.py`) to use the new category-aware event construction.
  - Update `SSEBridge` and frontend `useExecutionStatus` to filter/handle events by category.
  - Add `severity` and `recoverable` to alarm data classes.
  **Must NOT do**: Remove existing event type enum values (only reclassify and add fields; keep `VARIABLE_CHANGED` as deprecated alias for `MEASUREMENT_RECORDED`); change the EventBus publish/subscribe API; break backward compatibility with NATS wire format (the enum string value stays the same for the old name; new name has a new string value).
  **Parallelization**: Wave 2 | Blocked by: 1, loop-monaco-sse Task 0.1 | Blocks: 7, 8, 9, 10
  **References**:
  - `src/shared/events.py:1-160` — current EventType enum and 10 EventData classes
  - `src/ate_platform/scheduler/event_bus.py:40-120` — `publish()`, `subscribe()` with wildcards
  - `src/ate_platform/scheduler/scanner_scheduler.py:220-260` — event publishing points
  - `src/ate_cloud/nats/sse_bridge.py:45-100` — SSE event formatting
  - `frontend/src/composables/useExecutionStatus.ts:20-80` — frontend event handler
  - SEMI TEMS A4 standard — event/measurement/alarm categories (librarian research)
  - `tests/unit/scheduler/test_event_bus.py` — existing event bus tests
  **Acceptance criteria**:
  - `EventCategory` enum with `EVENT`, `MEASUREMENT`, `ALARM` values
  - Every `EventType` mapped to exactly one `EventCategory`
  - `VARIABLE_CHANGED` renamed to `MEASUREMENT_RECORDED` with `timestamp`, `unit`, `instrument_id` fields
  - Alarm events have `severity` and `recoverable` fields
  - `EventBus.publish()` validates category matches event type
  - Existing event bus tests pass with updated event construction
  - SSEBridge emits `event:` field matching category (SSE `event:` line = `event`, `measurement`, or `alarm`)
  **QA scenarios**:
  - Happy: `pytest tests/unit/scheduler/test_event_bus.py -v` — all events publish with correct category
  - Happy: `pytest tests/cloud/test_nats.py -v -k "event_category"` — NATS subscriber receives categorized events
  - Failure: Publish event with wrong category — expect ValueError from Event dataclass validation
  - Regression: `pytest tests/unit/scheduler/ -v` — all scheduler tests pass
  - Evidence: `.omo/evidence/task-6-architecture-optimization.json`
  **Commit**: Y | `refactor(events): reorganize event schema to TEMS A4 categories with severity and recoverability`

- [x] 7. Harden existing SSE heartbeat and fix JetStream replay pagination bug
  **What to do**: The SSE endpoint in `executions.py` already implements keep-alive (`: keep-alive\n\n` at L92) and Last-Event-ID replay (`replay_from_jetstream` at L70-77). Harden these existing mechanisms.
  - **Bug fix — JetStream replay pagination**: `replay_from_jetstream` fetches `batch=100` but doesn't paginate — if >100 events were missed, the rest are silently lost (L135). Fix: loop `fetch(batch=100)` until `StreamNotFoundError` or empty batch.
  - **Harden 1 — reduce heartbeat interval**: Change keep-alive from 30s to 15s (proxies like nginx default to 60s timeout; 30s is marginal).
  - **Harden 2 — add local-mode heartbeat**: When NATS is unavailable (local mode), the SSE bridge has no data source — add a heartbeat generator that yields `comment="keep-alive"` every 15s.
  - **Harden 3 — multi-client support**: Current `bridge.remove_queue(run_id)` on disconnect (L95) deletes the shared queue, breaking other clients subscribed to the same `run_id`. Fix: reference-count the queues — increment on `get_queue()`, decrement on `remove_queue()`, delete only when count reaches 0.
  - In `frontend/src/composables/useExecutionStatus.ts`: add `EventSource` reconnection with `lastEventId` persistence in `sessionStorage`; on reconnect, pass `Last-Event-ID` header.
  - Add connection status indicator: `status: "connected" | "disconnected" | "reconnecting"` reactive ref.
  **Must NOT do**: Change SSE to WebSocket; add client-to-server messaging; modify the NATS stream configuration.
  **Parallelization**: Wave 2 | Blocked by: 6 | Blocks: —
  **References**:
  - `src/ate_cloud/api/v1/executions.py:44-97` — `stream_execution_events` with keep-alive (L92) and replay (L70-77)
  - `src/ate_cloud/nats/sse_bridge.py:45-120` — `SSEBridge.start()`, `get_events()`, local mode, replay bug at L135
  - `src/ate_cloud/nats/subscriber.py:30-90` — `NATSSubscriber` JetStream pull consumer
  - `frontend/src/composables/useExecutionStatus.ts:10-40` — EventSource setup
  - `tests/cloud/test_nats.py` — existing NATS/SSE tests
  **Acceptance criteria**:
  - JetStream replay recovers 250 missed events correctly (pagination loop)
  - SSE keep-alive comment sent every 15s (not 30s) when idle
  - Local mode sends keep-alive every 15s
  - Two SSE clients for same `run_id` both receive events; disconnect of one doesn't break the other
  - Frontend stores `lastEventId` in sessionStorage on each event
  - On reconnect, frontend sends `Last-Event-ID` header; SSEBridge replays missed events
  - Connection status exposed in `useExecutionStatus.status` reactive ref
  **QA scenarios**:
  - Happy: `pytest tests/cloud/test_nats.py -v -k "sse_heartbeat"` — heartbeat at 15s interval
  - Happy: `pytest tests/cloud/test_nats.py -v -k "sse_reconnect"` — disconnect/reconnect replays >100 missed events
  - Happy: `pytest tests/cloud/test_nats.py -v -k "sse_multi_client"` — two clients, one disconnects, other unaffected
  - Failure: Set `max_replay_window=0`, verify no replay (but connection still succeeds)
  - Evidence: `.omo/evidence/task-7-architecture-optimization.json`
  **Commit**: Y | `fix(sse): harden heartbeat, fix replay pagination, add multi-client support`

- [x] 8. Add `skip_if` precondition to DSL for rule-based adaptive skipping
  **What to do**: Extend `YamlStep` and `YamlLoop` with a `skip_if` field that allows steps to be skipped based on variable expressions.
  - In `src/shared/dsl.py`: add `skip_if: str | None = None` to `YamlStep` and `YamlLoop`. Add `skip_reason: str | None = None` for logging.
  - In `src/ate_platform/scheduler/scanner_scheduler.py`: before dispatching a step, evaluate `skip_if` via `ConditionEvaluator.evaluate_skip_condition()`. If true, set step status to `SKIPPED` (new `StepStatus.SKIPPED`), log reason, and cascade to dependents (dependents whose preconditions include SKIPPED treat it as satisfied).
  - Add `StepStatus.SKIPPED` to `src/shared/types.py`.
  - In `frontend/src/types/dsl.ts`: add `skip_if?: string` to the generated TypeScript type.
  - In `frontend/src/views/SequenceEditor/components/PropertyPanel.vue`: add `skip_if` input field in the step property editor.
  - YAML example: `skip_if: "${power_on.voltage} > 5.0"` — skips overvoltage test if power_on already failed voltage check.
  **Must NOT do**: Implement ML-based skip prediction; add `skip_if` to conditions (separate from preconditions — skip_if is evaluated BEFORE execution, conditions are evaluated FOR execution).
  **Parallelization**: Wave 2 | Blocked by: 6, 2 | Blocks: —
  **References**:
  - `src/shared/dsl.py:15-35` — `YamlStep` dataclass with `condition` field
  - `src/ate_platform/scheduler/scanner_scheduler.py:155-200` — `compile_plan()`, step dispatch
  - `src/ate_platform/scheduler/condition_evaluator.py:40-90` — `evaluate()` for conditions
  - `src/shared/types.py:10-25` — `StepStatus` enum
  - `frontend/src/types/dsl.ts:15-40` — TypeScript YamlStep interface
  - `frontend/src/views/SequenceEditor/components/PropertyPanel.vue:80-130` — property form
  - DTA-QC Ericsson 2024 — adaptive testing 30-50% time reduction (librarian research)
  **Acceptance criteria**:
  - `YamlStep.skip_if` and `YamlLoop.skip_if` fields exist in DSL
  - `StepStatus.SKIPPED` added to enum
  - `ConditionEvaluator.evaluate_skip_condition()` evaluates `${}` expressions
  - When `skip_if` evaluates to `True`, step is set to SKIPPED, not executed, and dependents cascade
  - When `skip_if` evaluates to `False`, step dispatches normally
  - Frontend PropertyPanel shows `skip_if` input field
  - New test fixture: YAML plan with skip_if — verify step is skipped
  **QA scenarios**:
  - Happy: `pytest tests/unit/scheduler/test_scanner_scheduler.py -v -k "skip_if"` — step with true skip_if is skipped, dependents proceed
  - Happy: `pytest tests/unit/scheduler/test_scanner_scheduler.py -v -k "skip_if_false"` — step with false skip_if executes normally
  - Failure: `skip_if` expression references undefined variable — expect clear error message with variable name
  - Regression: `pytest tests/unit/scheduler/ -v` and `pytest tests/unit/dsl/ -v` — all tests pass
  - Evidence: `.omo/evidence/task-8-architecture-optimization.json`
  **Commit**: Y | `feat(dsl): add skip_if precondition for rule-based adaptive test skipping`

- [x] 9. Implement frontend state update batching (50ms window + dedup)
  **What to do**: Add a batching layer between SSE events and X6 graph updates to reduce reactive overhead.
  - In `frontend/src/composables/useExecutionStatus.ts`: replace direct `graph.setData()` calls with a `BatchBuffer` class.
  - `BatchBuffer` maintains a `Map<nodeId, Partial<NodeData>>`. `push(nodeId, data)` merges data. Every 50ms, `flush()` calls `graph.setData()` once with all accumulated changes, then clears the map.
  - Use `requestAnimationFrame`-aligned timer (not `setInterval`) to batch within a render frame.
  - Add deduplication: if same `nodeId` is updated multiple times within the window, only the latest data is applied.
  - Add `max_batch_size: 200` — if buffer exceeds 200 entries before 50ms, flush immediately.
  - Add `batchStats` reactive ref: `{ eventsReceived: number, batchesFlushed: number, avgBatchSize: number }` for debugging.
  **Must NOT do**: Change SSE event format; add throttling that drops events (all events must be processed, just batched); modify X6 graph rendering internals.
  **Parallelization**: Wave 2 | Blocked by: 6 | Blocks: —
  **References**:
  - `frontend/src/composables/useExecutionStatus.ts:40-100` — EventSource handler, setData calls
  - `frontend/src/views/SequenceEditor/components/GraphContainer.vue:50-120` — graph instance, node data binding
  - `frontend/src/models/nodes/types.ts` — NodeData interface
  **Acceptance criteria**:
  - `BatchBuffer` class with `push(nodeId, data)`, `flush()`, and configurable `windowMs` (default 50)
  - 10 rapid updates to same node within 50ms → only 1 `setData()` call
  - 10 rapid updates to 10 different nodes within 50ms → only 1 `setData()` call with all 10
  - Buffer exceeds `max_batch_size` → flushes immediately
  - `batchStats` reactive ref shows correct counts
  - Existing execution status tests pass with batching enabled
  **QA scenarios**:
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useExecutionStatus.test.ts` — batching produces correct final state
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/batchBuffer.test.ts` — new unit test for BatchBuffer
  - Failure: Inject 300 rapid updates, verify buffer flushes at max_batch_size=200 before 50ms
  - Regression: `cd frontend && npx vitest run` — all frontend tests pass
  - Evidence: `.omo/evidence/task-9-architecture-optimization.json`
  **Commit**: Y | `perf(frontend): add 50ms batch buffer for X6 node state updates`

### Wave 3: Frontend Completeness (P1)

- [x] 10. Complete loop container YAML ↔ Graph serialization (FOR/WHILE/FOREACH)
  **What to do**: Implement full bidirectional serialization for loop containers in `useSerializer.ts`.
  - In `graphToYaml()`: detect loop container nodes (check node.data.loopType); serialize to `YamlLoop` with `loop_type`, `condition`/`count`/`collection_expr`, `iteration_var`, `execution_mode`, and nested `steps` by recursing into the sub-graph.
  - In `yamlToGraphData()`: detect `YamlLoop` objects in the plan; create a parent loop node with embedded sub-graph geometry (nested children positioned relative to loop container). Set `node.data.loopType`, `node.data.condition`, etc.
  - Handle nested loops: infinite recursion guard (max depth 5), error on depth exceeded.
  - Edge handling: edges inside loop sub-graph use relative source/target IDs; loop container has incoming edges from predecessors and outgoing edges to successors (not children).
  - Update `SubGraphContainer.vue` to receive deserialized loop data and render the embedded X6 graph.
  **Must NOT do**: Change the YAML DSL schema (use existing `YamlLoop` from loop-monaco-sse Task 0.1); implement loop EXECUTION (handled by loop-monaco-sse); change StepLibraryPanel or drag-drop UX.
  **Parallelization**: Wave 3 | Blocked by: 6, loop-monaco-sse Task 0.1 | Blocks: —
  **References**:
  - `frontend/src/composables/useSerializer.ts:60-150` — `graphToYaml()`, `yamlToGraphData()` with "处理略" at L85-92
  - `frontend/src/types/dsl.ts:15-50` — YamlLoop interface
  - `frontend/src/views/SequenceEditor/components/SubGraphContainer.vue` — sub-graph component
  - `frontend/src/models/nodes/types.ts:20-50` — node type definitions including loopType
  - `tests/fixtures/loop_plan.yaml` — sample loop YAML fixture
  **Acceptance criteria**:
  - `graphToYaml()` produces valid `YamlLoop` with nested `steps` from loop sub-graph
  - `yamlToGraphData()` creates a loop container node with embedded children from YamlLoop
  - FOR loop serializes with `loop_type: "for"`, `count`, `iteration_var`
  - WHILE loop serializes with `loop_type: "while"`, `condition`
  - FOREACH loop serializes with `loop_type: "foreach"`, `collection_expr`, `iteration_var`
  - Nested loop (loop inside loop) serializes/deserializes correctly up to depth 5
  - Depth 6+ throws clear error message
  - Round-trip: YAML → graph → YAML produces identical YAML (ignoring formatting)
  **QA scenarios**:
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useSerializer.test.ts` — new test for all three loop types
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useSerializer.test.ts -t "nested_loop"` — nested loop round-trip
  - Failure: `cd frontend && npx vitest run src/composables/__tests__/useSerializer.test.ts -t "max_depth"` — depth 6 throws
  - Regression: `cd frontend && npx vitest run src/composables/__tests__/useSerializer.test.ts` — existing non-loop tests pass unchanged
  - Evidence: `.omo/evidence/task-10-architecture-optimization.json`
  **Commit**: Y | `feat(frontend): complete loop container YAML-to-Graph bidirectional serialization`

- [x] 11. Add dagre auto-layout for DAG-style test sequences
  **What to do**: Replace the fixed-coordinate grid layout in `yamlToGraphData()` with dagre DAG layout.
  - Install `dagre` and `@types/dagre` in frontend: `npm install dagre @types/dagre`.
  - New file `frontend/src/composables/useAutoLayout.ts`: `autoLayout(graphData: GraphData) -> GraphData` — uses dagre to compute node positions based on edge dependencies.
  - dagre config: `rankdir: "LR"` (left-to-right), `nodesep: 200`, `ranksep: 150`, `marginx: 20`, `marginy: 20`.
  - Node dimensions: estimate from node type (ScriptStepNode: 240×80, VariableNode: 200×60, LoopContainer: 300×120).
  - In `yamlToGraphData()`, replace `x: 100 + (index % 3) * 250, y: 100 + Math.floor(index / 3) * 150` with `autoLayout()` call.
  - Add `useAutoLayout` as optional: controlled by a toggle in Toolbar (default: auto-layout ON, user can switch to manual for custom positioning).
  **Must NOT do**: Install elkjs (dagre only); change the graph-to-YAML direction (auto-layout applies to deserialization only — manual positions in the editor are preserved during graph-to-YAML); auto-layout loop container children (loop children have relative coordinates within the container).
  **Parallelization**: Wave 3 | Blocked by: — | Blocks: —
  **References**:
  - `frontend/src/composables/useSerializer.ts:120-145` — fixed coordinate layout in `yamlToGraphData()`
  - `frontend/src/views/SequenceEditor/components/Toolbar.vue:20-60` — toolbar with existing toggles
  - `frontend/src/models/nodes/types.ts` — node dimension constants
  **Acceptance criteria**:
  - `dagre` installed and imported
  - `autoLayout()` positions nodes left-to-right, grouped by topological rank
  - 10-node chain: nodes laid out in a line (not a grid)
  - 10-node fan-out (A→B1...B9): A on left, B1-B9 on right, no overlaps
  - 3-level DAG: 3 distinct columns
  - Toolbar toggle switches between auto-layout and manual grid
  - Existing tests for non-layout logic pass unchanged
  **QA scenarios**:
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useAutoLayout.test.ts` — chain, fan-out, DAG layouts verified
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useAutoLayout.test.ts -t "no_overlap"` — no node bounding box overlaps
  - Failure: Empty graph → `autoLayout()` returns empty array (no crash)
  - Regression: `cd frontend && npx vitest run src/composables/__tests__/useSerializer.test.ts` — serializer tests pass
  - Evidence: `.omo/evidence/task-11-architecture-optimization.json`
  **Commit**: Y | `feat(frontend): add dagre auto-layout for DAG test sequence visualization`

- [x] 12. Offload cycle detection to Web Worker for >50 nodes
  **What to do**: Move DFS cycle detection from UI thread to a Web Worker to prevent blocking on large sequences.
  - New file `frontend/src/workers/dependencyCheck.worker.ts`: the same DFS algorithm from `useDependencyCheck.ts` but running in a `new Worker()`.
  - Worker receives `{ nodes, edges }` via `postMessage`, returns `{ hasCycle: boolean, cyclePath?: string[] }`.
  - In `useDependencyCheck.ts`: add threshold check — if `nodes.length > 50`, post to worker and await result via `onmessage` Promise; if ≤50, run on UI thread as before.
  - Add loading state: while worker is running, show a "Checking dependencies..." indicator.
  - Worker is created lazily (first use) and reused across dependency checks.
  - Add Vitest test that imports the worker logic directly (bypass `new Worker()` for testing) and tests the DFS algorithm.
  **Must NOT do**: Change the DFS algorithm (same logic, different thread); use SharedArrayBuffer (overkill for this use case).
  **Parallelization**: Wave 3 | Blocked by: — | Blocks: —
  **References**:
  - `frontend/src/composables/useDependencyCheck.ts:10-70` — current DFS cycle detection
  - `frontend/vite.config.ts` — Vite worker config (supports `?worker` import)
  **Acceptance criteria**:
  - `dependencyCheck.worker.ts` created with `onmessage` handler
  - `useDependencyCheck` uses worker for `nodes.length > 50`
  - 100-node graph with cycle → worker detects cycle, returns `hasCycle: true` with path
  - 100-node DAG → worker returns `hasCycle: false`
  - Loading state shown during worker computation
  - Worker reuses existing instance across multiple calls
  - Direct test of worker logic (no actual `Worker` thread) passes
  **QA scenarios**:
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useDependencyCheck.test.ts -t "large_graph"` — 100-node cycle detected
  - Happy: `cd frontend && npx vitest run src/composables/__tests__/useDependencyCheck.test.ts -t "worker"` — worker integration test
  - Failure: Worker throws unexpected error → caught and surfaced as `cycleError` in composable
  - Regression: `cd frontend && npx vitest run src/composables/__tests__/useDependencyCheck.test.ts` — existing tests pass
  - Evidence: `.omo/evidence/task-12-architecture-optimization.json`
  **Commit**: Y | `perf(frontend): offload cycle detection to Web Worker for >50 node sequences`

### Wave 4: Resilience Hardening (P2)

- [ ] 13. Add upload queue size and TTL pruning to SQLiteCache
  **What to do**: Add configurable limits to the upload queue to prevent disk exhaustion during prolonged NATS outages.
  - In `src/ate_platform/data/cache.py`: add `max_queue_size: int = 1000` and `max_queue_age_seconds: int = 3600` (1 hour) to `SQLiteCache.__init__`.
  - On `enqueue_upload()`, after inserting: `SELECT COUNT(*) FROM upload_queue`. If count > `max_queue_size`, `DELETE FROM upload_queue WHERE id IN (SELECT id FROM upload_queue ORDER BY created_at ASC LIMIT (count - max_queue_size))`.
  - On `enqueue_upload()` or as a periodic cleanup (every 60s): `DELETE FROM upload_queue WHERE created_at < datetime('now', '-${max_queue_age_seconds} seconds')`.
  - Log WARNING when pruning occurs: "Pruned N upload queue entries (size limit / age limit)".
  - Add `queue_stats()` method returning `{ current_size, oldest_entry_age, total_pruned }`.
  - Add settings in `ate_cloud` config: `UPLOAD_QUEUE_MAX_SIZE`, `UPLOAD_QUEUE_MAX_AGE_SECONDS` with defaults.
  **Must NOT do**: Delete entries that haven't been uploaded yet (only prune after age threshold); change the upload retry logic in ResumeManager.
  **Parallelization**: Wave 4 | Blocked by: 2 | Blocks: —
  **References**:
  - `src/ate_platform/data/cache.py:30-80` — `SQLiteCache.__init__`, `enqueue_upload()`, upload_queue table schema
  - `src/ate_platform/data/resume.py:20-70` — ResumeManager retry logic
  - `tests/unit/data/test_cache.py` — existing cache tests
  **Acceptance criteria**:
  - `SQLiteCache` accepts `max_queue_size` and `max_queue_age_seconds` parameters with defaults
  - Insert 1001 entries with `max_queue_size=1000` → oldest 1 entry pruned, warning logged
  - Insert entry, wait (or mock time), age exceeds `max_queue_age_seconds` → entry pruned on next enqueue or cleanup
  - `queue_stats()` returns accurate current_size and oldest_entry_age
  - Existing cache tests pass with new defaults
  **QA scenarios**:
  - Happy: `pytest tests/unit/data/test_cache.py -v -k "queue_pruning_size"` — size pruning works
  - Happy: `pytest tests/unit/data/test_cache.py -v -k "queue_pruning_age"` — age pruning works (mock time)
  - Failure: `max_queue_size=0` → raises ValueError during init
  - Regression: `pytest tests/unit/data/ -v` — all data layer tests pass
  - Evidence: `.omo/evidence/task-13-architecture-optimization.json`
  **Commit**: Y | `feat(cache): add size and TTL pruning to upload queue`

- [ ] 14. Add WatchDog health monitor for _scan_loop
  **What to do**: Create an independent asyncio health monitor that detects _scan_loop stalls.
  - New file `src/ate_platform/scheduler/watchdog.py`: `WatchDog` class with `start(interval=3.0)` — independent asyncio task that checks a shared `heartbeat_counter`. If counter hasn't incremented for `3 * scan_interval` (300ms default), logs CRITICAL, publishes `ALARM.HEARTBEAT_LOST` event, and calls `ScannerScheduler.emergency_shutdown()`.
  - In `scanner_scheduler.py`: increment `self._heartbeat` at the top of each `_scan_loop` iteration (or each `_tick()` call post-Task-1).
  - `emergency_shutdown()`: stop accepting new step dispatch, drain in-flight steps (wait for running steps to complete, max 30s), then set `_running = False`.
  - WatchDog runs in its own asyncio task, created at `ScannerScheduler.start()`, cancelled at `stop()`.
  - Deadlock detection moves from `_scan_loop` (current `scanner_scheduler.py:250-260`) to WatchDog: if deadlock detected, publish `ALARM.DEADLOCK_DETECTED` instead of silently returning.
  **Must NOT do**: Remove the deadlock detection logic — move it, don't delete it; add external monitoring dependencies (WatchDog is entirely in-process asyncio).
  **Parallelization**: Wave 4 | Blocked by: 1, 2 | Blocks: —
  **References**:
  - `src/ate_platform/scheduler/scanner_scheduler.py:202-260` — `_scan_loop` with deadlock detection at L250-256
  - `src/shared/events.py` — EventType enum (add HEARTBEAT_LOST)
  **Acceptance criteria**:
  - `WatchDog` class with `start()` and `stop()`
  - Heartbeat counter shared between ScannerScheduler and WatchDog
  - 3 missed heartbeats → CRITICAL log, HEARTBEAT_LOST alarm, emergency shutdown
  - Deadlock detection (100 consecutive no-progress scans) moved to WatchDog, publishes DEADLOCK_DETECTED alarm
  - WatchDog created and started in `ScannerScheduler.start()`, cancelled in `stop()`
  - Existing scheduler tests pass with WatchDog in place
  **QA scenarios**:
  - Happy: `pytest tests/unit/scheduler/test_watchdog.py -v -k "heartbeat_lost"` — simulate stalled loop, verify alarm and shutdown
  - Happy: `pytest tests/unit/scheduler/test_watchdog.py -v -k "deadlock_detected"` — simulate deadlock, verify alarm
  - Happy: `pytest tests/unit/scheduler/test_watchdog.py -v -k "normal"` — normal operation, no false alarms
  - Regression: `pytest tests/unit/scheduler/ -v` — all tests pass
  - Evidence: `.omo/evidence/task-14-architecture-optimization.json`
  **Commit**: Y | `feat(scheduler): add WatchDog health monitor with heartbeat and deadlock detection`

- [ ] 15. Add worker pool exhaustion detection and alarm
  **What to do**: Detect when all workers in ThreadPoolExecutor are busy and resource dependencies may cause deadlock.
  - In `src/ate_platform/executor/process_executor.py`: add `_active_count` atomic counter. `execute_async()` increments before submitting, decrements in `done_callback`.
  - Add `get_pool_utilization() -> float` returning `active / max_workers`.
  - In `scanner_scheduler.py` or `WatchDog`: on dispatch attempt, if `utilization >= 1.0` (all workers busy):
    - Check if the step's required resources are held by steps currently running (from `ResourceManager` active locks).
    - If YES: potential deadlock. Publish `ALARM.WORKER_EXHAUSTED` with `deadlock_risk: true`, `blocked_resources: [resource_ids]`, `holding_workers: [step_ids]`.
    - If NO: just log WARNING "Pool saturated, step queued".
  - Add `pool_stats()` to StepExecutor Protocol returning `{ active, max, utilization, queued }`.
  **Must NOT do**: Implement automatic resource preemption or forced release (P3+); change ThreadPoolExecutor to ProcessPoolExecutor.
  **Parallelization**: Wave 4 | Blocked by: 1, 2 | Blocks: —
  **References**:
  - `src/ate_platform/executor/process_executor.py:30-60` — ThreadPoolExecutor creation, `execute_async()`
  - `src/ate_platform/scheduler/resource_manager.py:40-90` — active resource locks, `get_active_locks()`
  - StepExecutor Protocol from Task 2
  **Acceptance criteria**:
  - `ProcessStepExecutor` tracks active worker count atomically
  - `get_pool_utilization()` returns correct ratio
  - Utilization at 100% + resource deadlock detected → ALARM published with holding_workers and blocked_resources
  - Utilization at 100% + no deadlock → WARNING logged only
  - `pool_stats()` on StepExecutor Protocol returns utilization data
  **QA scenarios**:
  - Happy: `pytest tests/unit/executor/test_step_executor.py -v -k "pool_exhaustion_alarm"` — simulate full pool with deadlock, verify alarm
  - Happy: `pytest tests/unit/executor/test_step_executor.py -v -k "pool_saturated_warning"` — full pool without deadlock, verify warning only
  - Failure: Pool of 1 worker, submit 2 steps — second step queues, no crash
  - Evidence: `.omo/evidence/task-15-architecture-optimization.json`
  **Commit**: Y | `feat(executor): add worker pool exhaustion detection with deadlock risk alarm`

- [ ] 16. Index failed test sequences in Qdrant for RAG failure diagnosis
  **What to do**: Extend the planned Qdrant RAG system to index failed test sequences alongside successful ones.
  - New file `src/ate_cloud/services/failure_indexer.py`: `FailureIndexer` class that listens for `STEP_FAILED` and `EXECUTION_COMPLETED` (with `result: FAILED`) events.
  - On failure, extract: `sequence_yaml` (the full sequence), `failed_step_id`, `failed_step_name`, `error_message`, `variable_snapshot` (all variable values at time of failure), `step_history` (ordered list of step status transitions).
  - Embed: concatenate `failed_step_name + error_message + variable_snapshot` → vector via DeepAgents embedding → store in Qdrant collection `ate_failures` with metadata payload (sequence_yaml, step_history).
  - Add `search_similar_failures(error_message: str, top_k: int = 5) -> list[FailureRecord]` for similarity search.
  - In Qdrant initialization (lifespan), create `ate_failures` collection with same vector dimensions as the existing `ate_sequences` collection.
  **Must NOT do**: Train ML models on failure data; implement automated remediation suggestions; change the Qdrant configuration from 实现方案.md.
  **Parallelization**: Wave 4 | Blocked by: — | Blocks: —
  **References**:
  - 实现方案.md — Qdrant integration plan, embedding pipeline
  - `src/shared/events.py` — STEP_FAILED, EXECUTION_COMPLETED events
  - `src/ate_cloud/nats/sse_bridge.py` — event ingestion point
  - Dual-Predictor method 2023 — indexing failures for predictive optimization (librarian research)
  **Acceptance criteria**:
  - `FailureIndexer` subscribes to STEP_FAILED and EXECUTION_COMPLETED events
  - Failed sequence metadata (yaml, step, error, variables, history) extracted and embedded
  - Stored in Qdrant `ate_failures` collection
  - `search_similar_failures("voltage out of range")` returns similar past failures
  - Indexing is non-blocking: failure does not impact the main execution flow
  - Qdrant collection created on startup if not exists
  **QA scenarios**:
  - Happy: `pytest tests/cloud/test_failure_indexer.py -v -k "index_failure"` — failed execution is indexed
  - Happy: `pytest tests/cloud/test_failure_indexer.py -v -k "search_similar"` — similarity search returns relevant past failures
  - Failure: Qdrant unavailable → failure is logged but execution continues (graceful degradation)
  - Evidence: `.omo/evidence/task-16-architecture-optimization.json`
  **Commit**: Y | `feat(ai): index failed test sequences in Qdrant for RAG-based failure diagnosis`

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit
- [ ] F2. Code quality review
- [ ] F3. Real manual QA
- [ ] F4. Scope fidelity

## Commit strategy
- One commit per task (16 commits total), each self-contained and passing tests
- Commit format: `type(scope): summary` — semantic commits
- Types: `refactor` (Tasks 1-3, 5), `feat` (Tasks 4, 6-7, 9-10, 12-15), `perf` (Tasks 8, 11)
- No merge commits within a wave; rebase each task on the wave's base
- Wave-level tag after each wave passes full test suite: `wave-1`, `wave-2`, etc.
- After final wave: tag `architecture-optimization-v1`
- PR review: each wave as a PR; must pass CI (pytest + Vitest + Ruff + mypy) before merge

## Success criteria
1. **Latency**: Step dispatch latency <10ms from event (was 0-100ms polling delay)
2. **Throughput**: 100-node test sequence completes with no individual step delayed by polling
3. **Driver extensibility**: New instrument driver added by implementing BaseDriver + BaseAbstraction, no SCPI knowledge needed in MAL layer
4. **Mock fidelity**: Auto-generated mocks pass all existing mock-based tests
5. **Event standardization**: Every event has a TEMS A4 category; consumers can filter by category
6. **SSE reliability**: Frontend reconnects within 5s and recovers all missed events
7. **Adaptive efficiency**: Test sequence with 50% skip_if steps runs in ~50% of original time
8. **Frontend performance**: 1000 events/sec processed with ≤20 X6 setData() calls/sec
9. **Serialization fidelity**: YAML → Graph → YAML round-trip produces identical YAML for all loop types
10. **Layout quality**: 100-node DAG laid out with no node overlaps and clear topological grouping
11. **Disk safety**: Upload queue never exceeds configured max size or age
12. **Scheduler resilience**: WatchDog detects stalled scan loop within 300ms and initiates shutdown
13. **Pool safety**: Worker pool exhaustion with deadlock risk generates alarm within 1s
14. **Failure diagnosis**: Similar past failures found via RAG within 500ms
15. **Regression safety**: All existing tests (pytest + Vitest) pass without modification (except intentional API changes)
