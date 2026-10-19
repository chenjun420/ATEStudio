# PA601-D54A 全量 commissioning 计划（23-TC 上板 bring-up）

> 计划文件：`data/commission/pa601_full_commission.yaml`
> 适用产品：PA601-D54A（54V 直流输出电源）
> 定位：**上板 bring-up 骨架**，逐项落地业界电源产测基准 TC-01..TC-23 的
> 测量判据与 `cond_fingerprint` 归因链。**不是可直接 dry-run 的迷你计划**
> （见下方"重要限制"）。

## 0. 重要限制（先读）

1. **不可直接 dry-run / 不可直接上板执行**：除 TC-10 复用既有
   `commission_measure_voltage.py` 外，其余 22 个 step 的 `script` 均为
   **TBD（待编写）**，仓内不存在对应 `.py`。
2. **解析器不校验脚本存在性**：`ate_platform.dsl.parser.YamlParser.parse()`
   只校验 `name` / `version` / `scope` / `step.id` / `step.script` 非空，
   **不检查 script 文件是否存在**（实测确认；`parse()` 与 `validate()` 均不做
   文件存在性校验）。故本 YAML 可被解析、`validate()` 返回空错误，但真机执行
   到 TBD 步骤会因脚本缺失由执行器 fail-fast 中止。
3. **占位限值**：`limit_source` 带【占位】的 TC（05/06/08/17/21/22）在基准
   表中无数值，已填合理占位。**正式上板前必须以 PA601 规格书替换全部占位值**。

### TBD 脚本清单

| TC | 脚本 | 状态 |
|---|---|---|
| TC-10 | `commission_measure_voltage.py` | **existing**（复用 mini plan 脚本） |
| 其余 22 项 | 见下表"脚本状态"列 | **TBD（待编写）** |

## 1. 23-TC 映射表

| TC# | 名称 | M-* 方法 | 仪器（resources） | 脚本 | 脚本状态 | 备注（限值 / 来源） |
|---|---|---|---|---|---|---|
| TC-01 | HIPOT 电介质强度 | M-HIPOT | HIPOT_1（chroma-19020/19032, keysight-e4980a） | `commission_hipot.py` | TBD | 1500Vac / 漏电流 ≤5mA【占位】；IEC 62368-1 |
| TC-02 | 绝缘电阻 | M-INSULATION-RES | IR_1（chroma-19020/19032, keysight-e4980a） | `commission_measure_insulation.py` | TBD | 500Vdc, ≥10MΩ（typ 100MΩ）；IEC 62368-1 |
| TC-03 | 接地连续性 | M-GROUND-BOND | BOND_1（chroma-19020/19032） | `commission_measure_ground_bond.py` | TBD | 25A, ≤0.1Ω；IEC 62368-1 |
| TC-04 | 接触电流/漏电流 | M-TOUCH-CURRENT | LEAKAGE_1（chroma-19032） | `commission_measure_touch_current.py` | TBD | ≤3.5mA AC；IEC 60990 |
| TC-05 | 上电/掉电时序 | —（缺，需补 M-STARTUP-TIME/sequencing） | AC_SRC_1 | `commission_power_sequence.py` | TBD | 【占位】基准未给数值；80 PLUS; Tek |
| TC-06 | 输入浪涌电流 | M-INRUSH | SCOPE_1（tek-mso46, chroma-61605） | `commission_measure_inrush.py` | TBD | 【占位】peak A within 50ms；ICE-3 5/8; UL |
| TC-07 | 掉电保持时间 | M-HOLDUP | AC_SRC_1, ELOAD_1 | `commission_holdup.py` | TBD | ≥16-20ms @80% nom. V；IEC 61000-4-11 |
| TC-08 | UVLO/OVLO 阈值 | M-UVLO（/M-UVP） | AC_SRC_1 | `commission_measure_uvlo.py` | TBD | 【占位】trip/recovery hysteresis；IEC 61204 |
| TC-09 | 功率因数/谐波 | M-PF | PWR_METER_1（yokogawa, hioki-pw3335） | `commission_measure_pf.py` | TBD | PF ≥0.9；IEC 61000-3-2 |
| TC-10 | 输出电压容差 | —（output_voltage kind） | DMM_1 | `commission_measure_voltage.py` | **existing** | 54V ±5% = 51.3..56.7V |
| TC-11 | 线性/负载调整率 | M-REGULATION | SCOPE_1, PWR_METER_1 | `commission_measure_regulation.py` | TBD | ≤1%；IEC 61204 |
| TC-12 | 交叉调整率 | —（缺，需补 M-CROSS-REG） | DMM_1 | `commission_measure_cross_reg.py` | TBD | ±2%（multi-rail） |
| TC-13 | 输出纹波与噪声 | M-RIPPLE-NOISE | SCOPE_1（tek-mso46, hp-34461a） | `commission_measure_ripple.py` | TBD | ≤1%·Vout（540mVpp）typ ≤100mVpp；IEC 61204-3 |
| TC-14 | 动态响应 | M-TRANSIENT | SCOPE_1, ELOAD_1 | `commission_measure_transient.py` | TBD | recovery ≤500µs；IEC 61204 |
| TC-15 | 启动/上升时间 | M-STARTUP | SCOPE_1（tek-mso46, gwinstek-psw） | `commission_measure_startup.py` | TBD | <100ms（sometimes）；通用 |
| TC-16 | 启动过冲 | —（缺，需补 M-OVERSHOOT） | SCOPE_1 | `commission_measure_overshoot.py` | TBD | ≤5%；通用 |
| TC-17 | 效率/功率损耗 | M-EFFICIENCY | PWR_METER_1（yokogawa, hioki-pw3335） | `commission_measure_efficiency.py` | TBD | 【占位】η curve 25/50/75/100%；80 PLUS; ETSI EN 303470 |
| TC-18 | 过流保护 OCP | M-OCP | ELOAD_1（chroma-63206, keysight-n6700） | `commission_ocp.py` | TBD | 110-150% rated I；IEC 62368-1 |
| TC-19 | 过压保护 OVP | M-OVP | ELOAD_1（chroma-62000h, keysight-n6700） | `commission_ovp.py` | TBD | 110-130% rated V, latch；IEC 61204-3 |
| TC-20 | 欠压保护 UVP | M-UVP | ELOAD_1 | `commission_uvp.py` | TBD | 80-90% rated V；IEC 61204 |
| TC-21 | 短路保护 SCP | —（SCP 经 OCP fallback；方法本体有 M-SCP） | ELOAD_1 | `commission_scp.py` | TBD | 【占位】continuous vs hiccup recovery；IEC 61204 |
| TC-22 | 过温保护 OTP | M-OTP | THERMO_1（温箱 + 热电偶/DAQ） | `commission_otp.py` | TBD | 【占位】thermistor trip；IEC 62368-1 cl.9 |
| TC-23 | 老化 Burn-In | M-BURN-IN | AGING_RACK_1, THERMO_1（老化柜） | `commission_burn_in.py` | TBD | 48-168h, 50°C, 满载（min 48h）；Tek; OCP 48V |

> 仪器 id 与 M-* 方法对应关系来自 MATRag
> `data/domain/source/power_test_methods.yaml` 与 `instrument_catalog.yaml`；
> TC 基准与缺口（TC-05/12/16 无 M-*，TC-22/23 原缺方法）来自
> `data/reports/end-to-end-gap-assessment-2026-10-19.md` §2/§3。

## 2. 真机执行

> **注意**：`scripts/commission_real_board.py` 当前只跑 **mini plan**（2 步
> 54V），**不加载本 23-TC 全量计划**。本计划的全量执行需先补齐 TBD 脚本，并由
> 后续调度入口（edge 直接分发全量 plan）驱动。下段给出该脚本的**真机命令形态**，
> 供上板时复用的数据链与参数约定。

### 2.1 mini plan 真机九步（现状可跑，链路验证）

```powershell
# 真机：直接打现场网络与设备池
.venv\Scripts\python.exe scripts\commission_real_board.py `
    --product PA601-D54A --fixture-id FX-0007 `
    --board-host 192.168.1.100 --mdc-url http://127.0.0.1:8000 `
    --psu-port 5025 --psu-device-id psu001 --dmm-device-id dmm001
```

- `--product PA601-D54A` → `_pick_mini_plan` 自动选
  `data/commission/mini_commission_pa601.yaml`（54V 版）。
- `--fixture-id FX-xxxx` 现场夹具编号（**真机模式必填**，缺失返回退出码 2）。
- `--board-host` 板地址（默认 `192.168.1.100`）。
- `--mdc-url` mat-device-control 基址（默认 `http://127.0.0.1:8000`）。
- 退出码：`0` 九步全 PASS；`1` 某步 FAIL 已 fail-fast；`2` 参数错误。

### 2.2 全量 23-TC 上板（补脚本后）

1. 按 §1 映射表补齐全部 **TBD** 脚本到 `tests/tmp/commission_run/`
   （与 `commission_real_board.py` 运行期生成约定一致），或改为随仓发布。
2. 用 PA601 规格书替换 YAML 中所有 `limit_source` 带【占位】的 `value` 限值。
3. 数据链前置（同 §2.1）：板 `--board-host` / `--mdc-url` / 设备表
   `--psu-device-id` / `--dmm-device-id` 与现场一致；安全回路就绪。
4. 以 edge 直接分发 `data/commission/pa601_full_commission.yaml`
   （`YamlParser().parse()` → `V32PlanDispatcher`），`on_failure: abort` 保证
   fail-fast；TC-23 老化步 `timeout: 0`（无超时，专用长时老化）。

### 2.3 前置与安全（摘要）

- 安全顺序：TC-02 绝缘电阻 / TC-03 接地连续性 **先于** TC-01 耐压
  （IEC 62368-1 安规顺序）。
- TC-01 高压作业：授权人员、隔离区、测试后强制放电。
- TC-18..TC-21 触发保护可能锁存：确认复位流程。
- TC-22/TC-23 温箱 + 长时满载：全程温度/电流监控。

## 3. 维护与来源

- 步骤字段结构对齐 `data/commission/mini_commission_pa601.yaml`。
- 本计划**不改**任何公共契约文件，也**不影响**既有多步 dry-run 链路
  （`scripts/commission_real_board.py` + `mini_commission_pa601.yaml`）。
- 解析契约：`ate_platform.dsl.parser.YamlParser`（`parse()` 不校验脚本存在性，
  `validate()` 校验 name/version/scope、step.id 唯一、script 非空、timeout/retry
  非负等；本计划 `validate()` 返回空错误列表）。

## 4. 虚拟执行（2026-10-19 第 12 波）

> 仅在本节成立：底有 9 台 **MAT.Virtual\*** 确定性虚拟仪器（详见
> `mat-device-control/modules/device/drivers/virtual/`），`required` 硬件尚未就位。
> 真机执行走 §2.2；硬件目录条目就位后即可不再走虚拟路径。

### 4.1 23 步脚本齐 + overlay 外置

22 个 TBD 脚本全部补齐到 `data/commission/scripts/commission_*.py`，每脚本：

- 自含 `_driver()` 走 `MatDeviceHttpDriver`（`device_id` / `base_url` 由 runner 注入）；
- 自含 `_judge(measured, value)`：当 `min/max` 皆非空且 `min>max` 时归一化为 `lo/hi`，
  单边/无边按语义判定；
- 支持 `params["force_measured"]`（故障注入），用于 runner 的 `--inject` 链路；
- 成功后设置 `result_measured_value / result_passed / result_cond_fingerprint / result_detail`；
  越限 `raise AssertionError`。

5 个占位 TC 的限值未直接改计划本体，外置于
`data/commission/pa601_virtual_limits.yaml`（`tc05 / tc06 / tc08 / tc21 / tc22`），
每条均有 `note` 说明「**虚拟基线值，上板前按 PA601 规格书替换**」。

### 4.2 runner：`scripts/commission_full_virtual.py`

三模式 CLI：

```powershell
# live：起临时 workdir + 启 mat-device-control uvicorn + 跑 23 步 → 23/23 PASS
.venv\Scripts\python.exe scripts\commission_full_virtual.py

# 静态自检：脚本齐/可编译/与 plan 一一对应 + overlay 覆盖完整 + plan 解析 23 步
.venv\Scripts\python.exe scripts\commission_full_virtual.py --check

# 故障注入：断言该 tc FAIL 且 fail-fast 中止，退出码 1
.venv\Scripts\python.exe scripts\commission_full_virtual.py --inject tc06=100.0
.venv\Scripts\python.exe scripts\commission_full_virtual.py --inject tc21=99.0
```

退出码：`0` = 23/23 PASS；`1` = 注入证 ok 或运行未全通过；`2` = 配置/注入链异常；`3` = MDC 虚拟驱动未注册。

报告路径：`data/reports/pa601-23tc-virtual-2026-10-19.md`（含逐步表、结论、免责声明）。

### 4.3 已知限制与上板迁移

- **虚拟值是「确定性合格基线」**，不替代真机与规格书。上板前必须以 PA601 规格书替换
  `pa601_virtual_limits.yaml` 的 5 条占位值，并删除该 overlay 文件或将 runner 改为
  「不读 overlay」。
- **连接字段**：`connection_type="virtual"` / `protocol="custom"` 集中在 runner 常量
  `VIRTUAL_CONNECTION_TYPE` / `VIRTUAL_PROTOCOL`。若 M 仓调整枚举，改这两处即可。
- **MDC API 白名单**：本仓 runner 生成的 `devices.toml` 每设备 `allowed_methods=[…]`
  已声明全部冻结方法；上板若新增仪器方法，在 `devices.toml` 或环境变量
  `MAT_DEVICE_METHOD_ALLOWLIST` 追加即可。
- 复用 `scripts/commission_real_board.py` 九步模式（dry-run 9/9 PASS 基线不变）。
