# 真机（.25 板 / 等价硬件）接入手册 —— Real-Board Commissioning Runbook

> 适用范围：把一块现场真板（.25 板或等价真机）接进
> **ATEStudio edge → MatDeviceHttpDriver → mat-device-control → SCPI(TCP) → 仪器**
> 的完整数据链，并逐步断言"哪里通、哪里断"。
>
> 交付物（本次）：
> - 可执行脚本：`scripts/commission_real_board.py`（九步 fail-fast）
> - 计划样本：`data/commission/mini_commission.yaml`（1 需求 / 2 步）
> - FILL_IN 工具：`scripts/fill_in_stub.py`
> - 本文档：`docs/commissioning/real-board-runbook.md`
>
> 本文档是给**硬件工程师在板卡到现场后按步执行**的操作手册，不是 CI 测试。
> 每一步的通过/失败条件、失败后的排查动作都写在下面。

---

## 0. 一句话结论（怎么用）

```powershell
# dry-run：本机自起 SCPI 反射服务 + mat-device-control，替代真板/真机
.venv\Scripts\python.exe scripts\commission_real_board.py `
    --product PA601-D54A --fixture-id test_fixture_001 --dry-run

# 真机：直接打现场网络与设备池
.venv\Scripts\python.exe scripts\commission_real_board.py `
    --product PA601-D54A --fixture-id FX-0007 `
    --board-host 192.168.1.100 --mdc-url http://127.0.0.1:8000 `
    --psu-port 5025 --psu-device-id psu001 --dmm-device-id dmm001
```

退出码：`0` = 九步全 PASS（数据链贯通）；`1` = 某步 FAIL 已 fail-fast 中止；
`2` = 参数错误（真机模式缺 `--fixture-id`）。**任一步失败即中止，不做静默降级。**

---

## 1. 数据链架构（真机）

```
 真板 .25 / 真机
   │  SCPI over TCP(SOCKET)  TCPIP0::<host>::5025::SOCKET
   ▼
 ┌─────────────────────┐   HTTP    ┌──────────────────────┐
 │ ATEStudio edge      │ ────────▶ │ mat-device-control   │
 │  MatDeviceHttpDriver │           │  POST /devices/{id}/ │
 │  (edge driver 层)    │ ◀──────── │        call          │
 │  BaseDriver 同步接口  │  envelope │  ResponseMiddleware  │
 └─────────────────────┘           │  → 驱动 → SCPI 会话   │
        │                          └──────────────────────┘
        │ outputs(result_*)                │
        ▼                                  ▼ 仪器 (PSU/DMM)
 ┌─────────────────────┐
 │ edge V32PlanDispatcher│  执行 mini plan 的 2 个脚本步骤
 └─────────────────────┘
        │ STEP_COMPLETED / EXECUTION_COMPLETED
        ▼
 ┌─────────────────────┐   Execution.step_results 落库
 │ ate_cloud relay      │ ─────────────────────────▶ DB(Execution)
 └─────────────────────┘
        │
        ▼
 audit_report.measured_conditions（cond_fingerprint 归因链）
```

关键契约（**不得在 commissioning 中改动**）：

- mat-device-control 统一响应封装：`{"success", "code", "message", "data": {"result", "error"}}`
  （`mat-device-control/modules/core/response.py` 的 ResponseMiddleware）。
  `MatDeviceHttpDriver._call` 先解 `data.result`，再兼容扁平 `result`。
- 设备方法名走 mat-device-control 的 `/devices/{id}/call` 白名单（含
  `connect/query/read/write/identify/set_voltage/measure_voltage/...`）。
- step outputs 约定：脚本模块级非下划线全局变量会被收集为 outputs
  （`result_measured_value` / `result_cond_fingerprint` / `result_passed` /
  `result_applied` / `FIXTURE_ID`）。

---

## 2. 硬件与软件前置条件（真机模式）

| 类别 | 前置条件 | 校验动作 |
|---|---|---|
| 网络 | 本机可 ping 通板址（默认 `192.168.1.100`）或至少 TCP 5025 可达 | `ping 192.168.1.100`；`Test-NetConnection 192.168.1.100 -Port 5025` |
| 仪器 | PSU/DMM 已上电、SCPI 已使能、`*IDN?` 有响应 | 裸 socket 发 `*IDN?\n`（脚本 Step 2 自动做） |
| 控制器 | mat-device-control 已在现场主机运行且 `/health` = healthy | `curl http://127.0.0.1:8000/health` |
| 设备表 | 现场 `devices.toml` 已登记 PSU/DMM 的 `device_id` 与 `resource_string` | 与 `--psu-device-id` / `--dmm-device-id` 对齐 |
| 夹具 | 现场夹具编号（人工填写，用于替换计划的 `FILL_IN_AT_COMMISSION`） | 作为 `--fixture-id` 传入 |
| 环境 | `MAT_DEVICE_BASE_URL` 指向现场 mat-device-control（可选，参数优先） | `--mdc-url` |

> dry-run 不需要以上任何硬件：脚本自起 SCPI 反射服务与 mat-device-control 子进程，
> 用动态端口、临时 `config.toml`/`devices.toml`（**绝不改仓内文件**）。

---

## 3. 九步操作流程

每一步：**做什么 → 通过判据 → 失败怎么办**。

### Step 1 —— 本机 → 板 网络连通性
- **做什么**：`ping <board-host>`；ICMP 被禁时退化为 TCP `<psu-port>` 探测。
- **通过判据**：ping 成功，或（ping 不通但）TCP 5025 可达。
- **失败排查**：网线/网口指示灯；`ipconfig` 看是否同网段；现场防火墙是否放行 5025；
  板是否已上电。dry-run 下此步改为"反射服务 TCP 可达"，恒通过。

### Step 2 —— VISA/TCPIP 直连 PSU（`*IDN?`）
- **做什么**：裸 socket 连 `<psu-host>:<psu-port>`，发 `*IDN?\n`，读回一行。
- **通过判据**：返回非空且形如 `厂商,型号,序列号,版本`。
- **失败排查**：端口错（LXI 常见 5025，VISA 用 `SOCKET` 而非 `INSTR`）；
  仪器 SCPI 未使能；`TCPIP0::<host>::<port>::SOCKET` 资源串写错。

### Step 3 —— mat-device-control 健康（`GET /health`）
- **做什么**：`GET <mdc-url>/health`，解 `data.status`。
- **通过判据**：`status == "healthy"`。
- **失败排查**：mat-device-control 未启动/端口不符/封装字段变化；看其日志。

### Step 4 —— PSU `*IDN?` 经 HTTP 反射
- **做什么**：`POST <mdc-url>/devices/<psu001>/call`，body
  `{"method": "query", "kwargs": {"command": "*IDN?"}}`。
- **通过判据**：解包 `data.result` 后为含逗号的 IDN 字符串。
- **失败排查**：设备表未登记 `psu001`；驱动未连上 SCPI（看 mat-device-control 侧会话）；
  `data.result` 缺失 → 检查是否真走了 ResponseMiddleware（真链路封装）。

### Step 5 —— DMM `*IDN?` 经 HTTP 反射
- **做什么/判据**：同 Step 4，换 `<dmm001>`（第二条设备路径）。
- **失败排查**：与 Step 4 相同；单独确认 DMM 资源串与 `device_id`。

### Step 6 —— 注入 FILL_IN 桩（`fixture_id` + `_driver()` 真链实例化）
- **做什么**：`fill_in_stub.build_fill_in(product, fixture_id)` 生成
  `tests/tmp/fill_in.json`，其中 `driver_snippet` 的 `_driver()` 返回
  `MatDeviceHttpDriver`（**真链路**，非仿真器），并在实例化端注入 `FIXTURE_ID`。
- **通过判据**：产物落盘且 `driver_snippet` 含 `MatDeviceHttpDriver`。
- **失败排查**：`fixture_id` 为空（fail-fast）；路径无写权限。
- **背景**：ATERag 生成脚本把"取本站仪表实例"留成 `FILL_IN` 桩（`_driver()` 抛
  `NotImplementedError`），夹具号留成 `FILL_IN_AT_COMMISSION`。上真机前必须补齐——
  这是**人工 commissioning 工序**，不是生成器能自动完成的事（见 §5）。

### Step 7 —— mini plan：edge → MatDeviceHttpDriver → mat-device-control → PSU
- **做什么**：`YamlParser` 解析 `data/commission/mini_commission.yaml`；
  按 `fill_in.json` 的 `_driver()` 片段**运行期生成**两个脚本
  （`commission_set_voltage.py` / `commission_measure_voltage.py`，不含 FILL_IN 桩）到
  运行目录；用 `V32PlanDispatcher(simulation=False, script_executor=...)` 经
  `ProcessExecutor` 在独立线程执行。
- **通过判据**：两步（`psu_set_5v` → `psu_measure_5v`）全 PASS，且测量步 outputs 含
  数值型 `result_measured_value`（本样本 5.0V）。
- **失败排查**：edge 无法连 `mdc-url`（`MAT_DEVICE_BASE_URL` / `--mdc-url`）；
  驱动解包失败（`data.result`）；测量值非数值（仪器返回格式）；超时（`--psu-port` 或
  仪器忙）。

### Step 8 —— `Execution.step_results[*].outputs.measured_value` 落库
- **做什么**：以 relay 的 `_process_message` 消费 `STEP_COMPLETED` /
  `EXECUTION_COMPLETED`（内存 SQLite `Execution`），把 Step 7 的 outputs 落库，
  再读回验证。
- **通过判据**：`step_results["psu_measure_5v"]["outputs"]["measured_value"]` 为数值。
- **失败排查**：relay 信封形态（nested `{type, data}`）；outputs 键名（须与
  `result_*` 归一）；relay 未处理该 step（`step_id` 不匹配）。

### Step 9 —— `audit_report.measured_conditions` 非空（cond_fingerprint 归因）
- **做什么**：`failure_indexer.audit_report(db, run_id)`，读 `step_results` 抽取
  `measured_conditions`（`cond_fingerprint → {measured_value, pass}`）。
- **通过判据**：`measured_conditions` 非空，且首条 `cond_fingerprint` 与计划中一致
  （本样本 `a1b2c3d4e5f60718`）。
- **失败排查**：**读数步 outputs 未带 `cond_fingerprint`** —— 这是已知数据缺口
  GAP-09（见 §5），本 mini 计划已显式带上以打通往归因链。

---

## 4. 故障处理（fail-fast 语义）

- 任一步抛异常 → 该步记 `FAIL`（含异常类型与消息）→ **立即中止**后续步骤，
  已启动的 dry-run 子进程在 `finally` 中统一 `terminate/kill`。
- 结束时打印分步清单与结论（`全 N 步通过` / `不可交付 —— M 步失败`），进程退出码
  `0/1`。
- **不吞错**：无"某步失败但继续跑"的路径；无默认值兜底。需要旁路时必须显式改脚本。
- 常见全局故障：
  - `FileNotFoundError: mat-device-control 仓库/venv 不可用` → 设
    `MAT_DEVICE_CONTROL_REPO` 指向含 `.venv` 与 `tests/scpi_reflect_server.py` 的仓库。
  - dry-run 启动超时 → 看运行目录下 `scpi_server.err` / `app.err`。

---

## 5. 已知数据缺口与缓解（关联 gap-master-diagnosis）

来源：`MATRag/data/reports/gap-master-diagnosis-2026-10-18.md`（下称"缺口诊断"）。

| 缺口 | 现象 | 对本手册的影响 | 本手册的缓解 |
|---|---|---|---|
| **GAP-09**（最严重） | 生产路径 ATERag 脚本的 `measured_value`/`cond_fingerprint` **到不了** `Execution.step_results[].outputs`（A.4 demo 是手工塞扁平事件喂 relay，未走真实 forwarder） | Step 8/9 若走真实 NATS→worker→forwarder 链路可能拿不到读数 | 本脚本以 relay `_process_message` 直驱（与 A.4 demo 同法）验证 **落库 + 归因**是否可行；mini 计划**显式带 `cond_fingerprint`**，证明"带上即可归因"。真实 forwarder 路径须另做修复后再复测。 |
| **GAP-04** | `Execution.status=RUNNING` 与 `started_at` 无人写 | 真机执行在云端不可见"进行中" | 本脚本为验证落库，直接以 `status="RUNNING"` 预建 `Execution`；生产侧修复（首个 `STEP_STARTED → RUNNING`）待主会话统一改。 |
| **GAP-03** | 只有 measure 步发射 `cond_fingerprint`；扁平非空指纹而缺 `measured_value` 会 fail-fast 抛错 | 若把指纹错放到 set 步会炸 | mini 计划只在测量步带指纹；set 步仅写 `result_applied`。 |
| **GAP-01** | 负轨判据 min/max 反向（根因在 MATRag 抽取层，非 emit 模板） | 负轨产品真机测量会被误判 FAILED | 本样本用 5V 正轨，规避；负轨产品 commissioning 前须先修上游或加"min≤max 归一"防御。 |

> **判据纪法**：以上缓解是"commissioning 期验证链路"的技术手段，**不等于**生产
> 路径已修。真实 NATS/forwarder 路径的 GAP-09 修复由主会话统一处理（本手册不改公共
> 契约文件）。

---

## 6. 真机 vs 仿真（dry-run）对比

| 维度 | 真机模式 | dry-run 模式 |
|---|---|---|
| 板/网络 | 真 `192.168.1.100`（ICMP/TCP） | 本地 SCPI 反射服务（`127.0.0.1:<动态端口>`） |
| 仪器 | 真 PSU / DMM | `tests/scpi_reflect_server.py`（每连接独立会话，伪装 `*IDN?`/set/measure） |
| 控制器 | 现场 mat-device-control | 脚本自起 mat-device-control 子进程（临时 `config.toml`/`devices.toml`） |
| 设备表 | 现场 `devices.toml` | 临时副本（**绝不改仓内 `devices.toml`**） |
| 测量来源 | 独立 DMM 读取真值 | **同一 PSU 会话的回声读数**作 DMM 替身（见下） |
| Step 1 | ping/ TCP 真探测 | 反射服务 TCP 可达即通过 |
| 端口 | 固定 5025 / 8000 | 动态空闲端口，避免与本机服务冲突 |
| 依赖 | 真硬件 + 现场网络 | 仅需 mat-device-control 仓库与 venv |

**DMM 替身说明**：`scpi_reflect_server` 的每个 TCP 连接是**独立会话**，测量状态随连接
隔离。为了让"设定 → 测量"在同一次通电周期内可回声，dry-run 的测量步复用 **PSU 同一
会话** 作 DMM 替身；计划里测量步仍以 `resources: [DMM_1]` 标注（真机上应指向独立
DMM 通道）。**真机测量必须以独立 DMM 为准。**

---

## 7. 收尾与留痕

- 产物：
  - `tests/tmp/fill_in.json`（FILL_IN 桩补齐结果，含 `fixture_id` 与 `_driver()`）
  - `tests/tmp/commission_run/`（运行期生成脚本 + dry-run 子进程日志，可丢弃）
- 现场记录建议：产品号 / 夹具号 / 板序列号 / 九步结果 / 失败步与排查结论。
- **纪律**：本流程不改仓内 `devices.toml`；不改公共契约；失败即停，不静默降级。

---

## 8. 维护者备注（不在现场执行清单内）

- dry-run 的本地服务组镜像了
  `tests/integration/test_mat_device_http_driver_e2e.py` 的启动模式；
  若该 e2e 的启动参数变化，`_start_stack` 需同步。
- `scripts/fill_in_stub.py` 可在真机模式外单独使用（生成/打印 `fill_in.json`）：
  ```powershell
  .venv\Scripts\python.exe scripts\fill_in_stub.py `
      --product PA601-D54A --fixture-id FX-0007 `
      --base-url http://127.0.0.1:8000 --device-id psu001
  ```
- Step 7 的 edge 分发以 `simulation=False` + 注入 `script_executor` 运行；`_driver()`
  在脚本 worker 线程内同步调用 `MatDeviceHttpDriver`，其内部把异步 HTTP 桥接到私有时
  事件循环（见 `http_driver.py::_run`），与 edge 的事件循环互不阻塞。
