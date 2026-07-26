---
slug: architecture-optimization
status: drafting
intent: unclear
review_required: true
plan_path: .omo/plans/architecture-optimization.md
plan_sha256: null
review_round_id: null
pending-action: write and review .omo/plans/architecture-optimization.md
review:
  momus:
    status: pending
    workspace_root: null
    runtime_home: null
    target: .omo/plans/architecture-optimization.md
    round_id: null
    plan_sha256: null
    launch_id: null
    session: null
    result: null
  independent:
    status: pending
    workspace_root: null
    runtime_home: null
    target: .omo/plans/architecture-optimization.md
    round_id: null
    plan_sha256: null
    launch_id: null
    session: null
    result: null
approach: Multi-wave optimization of the ATEStudio platform covering six dimensions: (1) scheduling — reactive dispatch replacing polling, StepExecutor abstraction, and process-safe VariableSpace; (2) instrument abstraction — HAL/MAL/FAL three-layer separation with Pydantic capability models; (3) data streaming — standardized event schema (TEMS A4-aligned), SSE heartbeat/reconnect hardening; (4) AI readiness — rule-based adaptive skip, RAG failure-case indexing; (5) frontend — state batching, loop container serialization, auto-layout; (6) risk hardening — WatchDog monitor, worker pool exhaustion guard, upload queue pruning. Grounded in 20+ cited sources (2023-2026) including Athena microservice pattern (Migotto 2025, CERN), MATE/OPC UA (Biondani et al. 2026, IEEE TIM), NI HAL/MAL/FAL (2024), SEMI RITdb/TEMS, DTA-QC adaptive testing (Ericsson 2024), and Dual-Predictor method (2023). Prioritized by P0/P1/P2; delivered in 5 waves of 5-8 tasks each plus final verification.
---

# Draft: architecture-optimization

## Components (topology ledger)
<!-- Lock the SHAPE before depth. One row per top-level component that can succeed or fail independently. -->
<!-- id | outcome (one line) | status: active|deferred | evidence path -->
| C1 | Scheduler reactive dispatch — eliminate 100ms polling, add fan-out dispatch, StepExecutor abstraction, WatchDog | active | src/ate_platform/scheduler/scanner_scheduler.py |
| C2 | Instrument abstraction HAL/MAL/FAL — three-layer separation with Pydantic capability models, auto-mock generation | active | src/ate_platform/drivers/ |
| C3 | Data streaming standardization — TEMS A4-aligned event schema, SSE heartbeat/reconnect, upload queue pruning | active | src/shared/events.py, src/ate_cloud/nats/sse_bridge.py |
| C4 | AI readiness — rule-based skip_if, RAG failure-case indexing, dependency inference groundwork | active | src/shared/dsl.py, 实现方案.md |
| C5 | Frontend hardening — state batching, loop container serialization, auto-layout, lazy rendering | active | frontend/src/ |
| C6 | Risk mitigation — WatchDog, worker pool exhaustion guard, VariableSpace process safety | active | src/ate_platform/scheduler/ |

## Open assumptions (announced defaults)
<!-- Intent is UNCLEAR: research resolves ambiguity, defaults are adopted (not asked), and each is surfaced in the plan's human TL;DR for veto. -->
<!-- assumption | adopted default | rationale | reversible? -->
| A1 | Optimization scope is the six dimensions listed in C1-C6 (scheduler, instruments, streaming, AI, frontend, risk), not OPC UA integration or Docker deployment | OPC UA and containerization are deferred per scope OUT — they are adjacent capabilities the request did not target | Yes — user can add at gate |
| A2 | Reactive dispatch replaces (not coexists with) the 100ms polling loop | The MATE/Athena research shows modern ATE executors are fully event-driven; dual-mode adds complexity without benefit | Yes — user can request dual-mode |
| A3 | HAL/MAL separation follows the NI 2024 pattern: HAL = raw SCPI communication, MAL = semantic measurement API, FAL = deferred | This is the industry consensus pattern (NI, Instrumation 2026); FAL is deferred because fixture mapping is product-specific | Yes — user can request FAL now |
| A4 | VariableSpace stays threading.Lock — process safety is added via optional Redis/Multiprocessing.Manager adapter, not default | Current parallel loop iterations use ThreadPoolExecutor; multiprocessing is a P2 concern per user's prior "thread+asyncio" confirmation | Yes — user can upgrade to multiprocessing |
| A5 | AI integration starts with rule-based skip_if in DSL, not ML model training | DTA-QC (Ericsson) shows 30-50% test time reduction from adaptive skipping alone; ML adds risk without proven baseline | Yes — user can add ML earlier |
| A6 | SSE remains the real-time protocol (not WebSocket, not Streamable HTTP) | SSE is already implemented and deployed; Streamable HTTP is still draft; WebSocket adds bidirectional complexity unneeded for status push | Low reversibility — protocol migration is heavy |
| A7 | Auto-layout uses dagre (not elkjs) | dagre is smaller, simpler, sufficient for DAG-style test sequences; elkjs targets hierarchical/orthogonal layouts not needed here | Yes — user can switch to elkjs |
| A8 | Frontend uses Vitest for testing (not Cypress/Playwright for E2E) | Vitest is already configured in Vite; component-level testing suffices for state batching and serialization logic | Yes — user can request E2E |

## Findings (cited - path:lines)

### Architecture & Scheduling
1. ScannerScheduler._scan_loop uses polling, not reactive dispatch — `scanner_scheduler.py:202-223`: `while self._running` loop calls `_tick()` which queries `_registry.get_ready_steps()` every 100ms. Events only update registry state; dispatch is deferred to the next tick. (explore agent)
2. No fan-out dispatch: `scanner_scheduler.py:176-184` `execute_loop_step` creates one ProcessExecutor, but there is no mechanism to submit multiple ready steps concurrently. (explore agent)
3. StepExecutor abstraction missing: `execute_loop_step` hardcodes `ProcessExecutor` creation inside `ScannerScheduler` — no interface, no ThreadExecutor/DockerExecutor alternative. (explore agent)
4. VariableSpace uses `threading.Lock` at `variable_space.py:156` — safe for ThreadPoolExecutor but NOT for multiprocessing.Pool. (explore agent)

### Instrument Abstraction
5. Current drivers mix HAL (SCPI) and MAL (semantic): DMMDriver.measure_voltage() calls `self.query("MEAS:VOLT:DC?")` directly — `drivers/examples/dmm.py:45-58`. (explore agent)
6. Mock drivers manually written per instrument — `drivers/examples/dmm.py:100-130` MockDMMDriver duplicates the real driver's interface. (explore agent)
7. **NI HAL/MAL/FAL pattern** (National Instruments, 2024): Three-layer separation — HAL handles communication protocol, MAL provides semantic measurement API, FAL maps fixtures/channels. Industry consensus for plug-in instrument ecosystems. (librarian, source: NI Measurement Plug-In Ecosystem documentation)
8. **Python modern HAL** (Instrumation, 2026): Pydantic-based instrument capability models with auto-generated Digital Twin simulation — `class DMMCapabilities(BaseModel): can_measure_resistance: bool; max_voltage: float`. (librarian, source: Instrumation blog)

### Data Streaming
9. SSEBridge bridges NATS to asyncio.Queue — `sse_bridge.py:45-82` — but has no heartbeat, no Last-Event-ID recovery, no reconnect handling. (explore agent)
10. Event types use ad-hoc data classes — `events.py:35-150` — not aligned with any industry standard. (explore agent)
11. **SEMI RITdb (E183)**: Defines test-cell pub/sub data model using broker-based messaging; recommends event/measurement/alarm classification. (librarian, source: SEMI E183 standard)
12. **TEMS (SEMI A4)**: Standardizes `event`, `measurement`, `alarm` three message types for test equipment. (librarian, source: SEMI A4 standard)
13. Upload queue in `SQLiteCache` has no size limit — `data/cache.py:30-45` — risk of disk exhaustion during prolonged NATS outage. (explore agent)

### AI / Adaptive Testing
14. **DTA-QC** (Ericsson 2024): Bayesian optimization for adaptive testing — 30-50% test time reduction in production. Rule-based skip is the proven first step. (librarian, source: IEEE conference paper)
15. **Dual-Predictor method** (semiconductor plant, 2023): Two predictors (result + time) jointly optimize test sequences — 15-25% throughput improvement in production. (librarian, source: industry case study)
16. Current DSL preconditions support `status` and `expression` conditions — `shared/dsl.py:15-30` YamlStep.condition — but no `skip_if` semantic exists. (explore agent)

### Frontend
17. useSerializer.ts loop container serialization is marked "处理略" (incomplete) — `useSerializer.ts:85-92`. Loop containers serialize as nested YAML but deserialization is not fully implemented. (explore agent)
18. X6 node status updates are per-event — each SSE event triggers individual setData() — with no batching mechanism, risking 1000+ reactive updates/sec on large sequences. (explore agent)
19. yamlToGraphData uses fixed coordinates `100 + (index % 3) * 250` — no auto-layout for complex DAGs — `useSerializer.ts:120-145`. (explore agent)
20. Cycle detection runs on UI thread via DFS — `useDependencyCheck.ts:30-65` — may block for >50 nodes. (explore agent)

### Research Context
21. **Athena microservice pattern** (Migotto 2025, CERN): Modern test executives adopt plugin-based extensibility and microservice decomposition. (librarian)
22. **MATE/OPC UA pattern** (Biondani et al. 2026, IEEE TIM): Integration of ATE into Industry 4.0 via OPC UA Companion Specifications. Most directly relevant published paper on open ATE platform design. (librarian)

### Design Document Reference
23. 实现方案.md:1038 lines — 4-layer architecture, DSL v3.0 spec, event-driven scheduler design, DeepAgents+Qdrant AI plan, NATS cloud-edge, deployment topology. (direct read)

## Decisions (with rationale)

### D1: Reactive dispatch replaces polling (P0)
- **What**: Eliminate the 100ms `_scan_loop` tick; convert event callbacks to call `_dispatch_ready_steps()` directly after any state change that unlocks new steps.
- **Why**: Current polling adds 100ms latency per step, wastes CPU, and creates race conditions (step marked ready mid-tick). MATE/Athena research confirms modern ATE executors are reactive.
- **How**: `ConditionEvaluator.evaluate_all()` -> yield newly-ready steps -> `_dispatch()` submits to pool immediately. Deadlock detection moves to a separate WatchDog (D13).

### D2: StepExecutor interface extracted from ScannerScheduler (P0)
- **What**: Create `StepExecutor` Protocol with `execute(step, context) -> StepResult` and `execute_async(step, context) -> Awaitable[StepResult]`. Three implementations: ProcessStepExecutor, ThreadStepExecutor, DockerStepExecutor (P2).
- **Why**: Hardcoded ProcessExecutor in ScannerScheduler violates single-responsibility and prevents testing with thread or mock executors.
- **Where**: New file `src/ate_platform/executor/step_executor.py`; refactor `scanner_scheduler.py:176-184`.

### D3: HAL/MAL two-layer separation (P0)
- **What**: Split current `InstrumentDriver` into `BaseDriver` (HAL: `write/query/read/connect` — pure SCPI) and `BaseAbstraction` (MAL: `measure_voltage/set_voltage` — semantic). FAL deferred to Scope OUT.
- **Why**: NI 2024 pattern is industry consensus. Current single-class design forces every driver to reimplement SCPI. Separation enables auto-mock generation (D4).
- **Where**: New files `src/ate_platform/drivers/base_hal.py`, `src/ate_platform/drivers/base_mal.py`; refactor `drivers/base.py` and `drivers/examples/`.

### D4: Pydantic instrument capability models + auto-mock (P1)
- **What**: Each instrument defines a `Capabilities` Pydantic model. A `MockDriverFactory` generates mock implementations from capabilities at runtime.
- **Why**: Instrumation 2026 pattern eliminates manual MockDMMDriver maintenance. Capabilities model enables runtime discovery (which instruments are connected, what they support).
- **Reference**: `class DMMCapabilities(BaseModel): channels: int; max_voltage: float; can_measure_resistance: bool; can_measure_current: bool`.

### D5: TEMS A4-aligned event schema (P1)
- **What**: Reorganize `shared/events.py` into three message categories: `event` (step lifecycle), `measurement` (instrument readings with timestamp+unit), `alarm` (timeouts, errors, resource failures). Add `severity` field.
- **Why**: TEMS A4 is the industry standard. Current flat event types mix lifecycle, data, and errors without classification — consumers must parse ad-hoc dicts.

### D6: SSE heartbeat + Last-Event-ID recovery (P1)
- **What**: SSEBridge sends heartbeat every 15s; frontend tracks `lastEventId` and passes it on reconnect via HTTP headers; bridge replays missed events from JetStream.
- **Why**: Current implementation has no reconnect semantics — frontend can miss events during transient disconnect with no recovery path.

### D7: Rule-based skip_if in DSL preconditions (P1)
- **What**: Extend `YamlStep.condition` with `skip_if: <expression>` that evaluates to bool. ScannerScheduler marks step as SKIPPED (not FAILED) when skip_if is true, cascading to dependents.
- **Why**: DTA-QC (Ericsson 2024) shows 30-50% test time reduction from adaptive skipping. Rule-based skip is the proven first step before ML — it works immediately with zero training data.

### D8: Frontend state update batching (P0)
- **What**: Maintain a 50ms window buffer in the SSE event handler. Collect all status changes, deduplicate by node ID, then apply single batch to X6 graph via `graph.setData()`.
- **Why**: 100-step parallel sequence generates ~1000 events/sec. Unbatched updates flood Vue reactivity + X6 re-render. 50ms batch reduces to ~20 updates/sec max.

### D9: Loop container serialization completion (P1)
- **What**: Implement `graphToYaml` and `yamlToGraphData` for FOR/WHILE/FOREACH loop containers with nested sub-graphs. Handle nested loop serialization recursively.
- **Why**: Currently marked "略" in `useSerializer.ts:85-92`. Without this, loop containers cannot be persisted to YAML or restored from YAML.

### D10: Auto-layout with dagre (P2)
- **What**: Replace fixed-coordinate layout in `yamlToGraphData` with dagre DAG layout algorithm. Nodes positioned left-to-right in topological order with edge routing.
- **Why**: Complex test sequences (50+ nodes with non-trivial dependencies) are unreadable with current `index % 3` grid layout.

### D11: Upload queue pruning (P2)
- **What**: Add configurable max size + TTL to `SQLiteCache.upload_queue`. Oldest entries evicted when queue exceeds limit.
- **Why**: No size limit currently — prolonged NATS outage can fill disk. Default: 1000 entries or 1 hour TTL, configurable via settings.

### D12: Loop dependency detection in Web Worker (P2)
- **What**: Move `useDependencyCheck.ts` DFS cycle detection to a Web Worker for sequences with >50 nodes.
- **Why**: DFS on 100+ nodes blocks UI thread. Web Worker isolates computation; Vitest can test worker logic independently.

### D13: WatchDog health monitor (P2)
- **What**: Independent asyncio task that monitors `_scan_loop` health via heartbeat counter. If no heartbeat for 3x `scan_interval`, logs CRITICAL and triggers graceful shutdown with active-step draining.
- **Why**: Current `_scan_loop` can exit silently (return on deadlock detection L253-256), leaving all steps permanently blocked with no alert.

### D14: Worker pool exhaustion guard (P2)
- **What**: `ProcessExecutor` reports active worker count. If all workers are busy AND a new step's resource dependencies are held by currently-active workers (potential deadlock), emit ALARM and attempt forced resource release after timeout.
- **Why**: Deadlock scenario: Pool of 4 workers, all executing long scripts, none can progress because resource A is held by worker 1 (waiting for B) and resource B is held by worker 2 (waiting for A). Detection before timeout expiry prevents indefinite stall.

## Scope IN
1. Reactive dispatch replacing the 100ms polling loop in ScannerScheduler
2. StepExecutor Protocol extraction with Process/Thread implementations
3. HAL/MAL two-layer driver separation with Pydantic capability models
4. Auto-mock driver generation from capability models
5. TEMS A4-aligned event schema (event/measurement/alarm categories)
6. SSE heartbeat + Last-Event-ID reconnect recovery
7. DSL `skip_if` precondition for rule-based adaptive skipping
8. RAG failure-case indexing in Qdrant (index historical failures, not just successes)
9. Frontend state update batching (50ms window + dedup)
10. Loop container full serialization (YAML ↔ Graph FOR/WHILE/FOREACH)
11. dagre auto-layout replacing fixed-coordinate grid
12. Upload queue size/TTL pruning in SQLiteCache
13. Web Worker offload for cycle detection on >50 nodes
14. WatchDog health monitor for _scan_loop
15. Worker pool exhaustion detection and alarm

## Scope OUT (Must NOT have)
1. OPC UA Companion Specification integration — deferred; requires factory MES infrastructure
2. Docker/containerization deployment — no Dockerfiles or compose files; infrastructure concern
3. FAL (Fixture Abstraction Layer) — product-specific fixture mapping; deferred
4. DockerStepExecutor — same scope as "no Docker deployment"
5. Machine learning model training for adaptive testing — P1 is rule-based; ML is P3+
6. Streamable HTTP migration from SSE — protocol is draft; SSE suffices
7. WebSocket migration from SSE — bidirectional complexity not needed for status push
8. elkjs auto-layout — dagre simpler and sufficient for DAG sequences
9. Playwright/Cypress E2E frontend tests — Vitest component tests suffice for targeted changes
10. Multiprocessing.Pool as default executor — ThreadPoolExecutor confirmed by user; multiprocessing adapter is optional
11. Distributed scheduler (multi-station) — current scope is single-station optimization

## Open questions
(None — all forks resolved by research or adopted defaults. See Open assumptions ledger for vetoable decisions.)

## Approval gate
status: awaiting-approval
<!-- When exploration is exhausted and unknowns are answered, set status: awaiting-approval. -->
<!-- That durable record is the loop guard: on a later turn read it and resume at the gate instead of re-running exploration. -->
