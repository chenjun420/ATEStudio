# 操作手册：ATERag 规格评审流程（.24 板卡）

面向**操作这套系统的人**：怎么登录、谁能做什么、评审怎么走、怎么确认它真的
能用、以及出问题时先看哪里。

不覆盖安装与开通 —— 那是 [`部署手册-192.168.5.24调试服务器.md`](部署手册-192.168.5.24调试服务器.md)
的事。ATERag 侧的注记与导出流程在 ATERag 仓库的 `docs/使用说明.md`。

---

## 1. 访问

| 项 | 值 |
|---|---|
| 地址 | `http://192.168.5.24:8000` |
| 规格评审向导 | `http://192.168.5.24:8000/aterag-review` |
| API 文档 | `http://192.168.5.24:8000/docs` |
| 首个管理员 | `e2e-operator` / 初始密码 `admin` |

> **初始密码 `admin` 是弱口令，且服务在产线 LAN 上以明文 HTTP 暴露。**
> 首次登录后立即改掉：`PUT /api/v1/users/{自己id}` `{"password": "..."}`，
> 或用 `scripts/bootstrap_admin.py --username e2e-operator --password <新密码>`。
> 改密码**不会**让已签发的 token 失效（见 §6）。

板卡服务由 systemd 托管，四个单元：

```bash
sudo systemctl status ate-cloud aterag-mcp nats qdrant
```

---

## 2. 账号与权限

### 2.1 角色对应哪些权限

| 角色 | 拿到的 scope | 能做什么 |
|---|---|---|
| `admin` | `admin` `read` `write` `execute` **`aterag:import`** | 全部，含导入 bundle |
| `write` | `read` `write` | 改数据，**不能**导入 |
| `read` | `read` | 只读，含条件评审面 |
| `execute` | `execute` | 执行 |

`aterag:import` **只有 admin 有**，而且是刻意的：导入会覆盖规格书派生的权威行、
可能让已评审的条件失效，这与「改一条记录」不是同一个动作。

> 踩过的坑：这个 scope 曾被端点要求、却被**所有**角色（含 admin）都不授予，
> 结果向导第 1/4/5 步对所有人 403，而第 3 步（只需 read）正常 —— 功能看起来是
> 好的。现在有测试从端点源码反扫所有 `require_scopes(...)`，新加的受保护端点若
> 没被某个角色授予就会红。见 `tests/cloud/test_scope_reachability.py`。

### 2.2 用户由管理员创建

界面：**用户管理**页。底层端点：

| 操作 | 端点 |
|---|---|
| 列出 | `GET /api/v1/users`（需 `admin`） |
| 建用户并指定角色 | `POST /api/v1/users` |
| 改角色 / 改密码 / 停用 | `PUT /api/v1/users/{id}` |
| 删除 | `DELETE /api/v1/users/{id}` |

实测（.24，17/17 通过）：`read` 用户列用户、建用户、删用户**全部 403**，无法自行
提权成 admin；`write` 用户能读条件评审面但调导入规划 403。

### 2.3 admin 账号不可删除

`DELETE` 一个 `admin` 角色的账号一律 **403**，不论有几个 admin、也不论调用者是谁。

403 的提示里写明了可行路径：**先降级，再删除**（`PUT {"role":"read"}` → `DELETE`）。
降级是显式动作；删除不该顺手带走唯一的 `aterag:import` 持有者。

> **已知未堵的洞**：把最后一个 admin 用 `PUT {"role": ...}` 降级，仍会造成同样
> 的锁死。「admin 不可删除」只对 DELETE 这一个动词成立。要一并堵住请提。

不存在的账号返回 **404** 而不是 403 —— 否则「这个 id 存不存在」会变成 admin 专属
的探测接口。

### 2.4 首个管理员 / 改密码

```bash
cd /opt/atestudio
set -a && . ./.env && set +a

# 建管理员
.venv/bin/python scripts/bootstrap_admin.py --username <用户名> --role admin \
    --password <密码>

# 改密码（账号已存在时也生效）
.venv/bin/python scripts/bootstrap_admin.py --username <用户名> \
    --password <新密码>

# 只改角色，不要动密码
.venv/bin/python scripts/bootstrap_admin.py --username <用户名> --role read --promote
```

密码也可从环境变量给，避免进 shell history：`ATE_BOOTSTRAP_PASSWORD=<密码>`。

这是**部署期命令**，不是运行时后门 —— 请求路径上没有任何「第一个注册的人自动是
管理员」这类逻辑，管理员建好之后就不需要它了。

---

## 3. 板卡必需配置

`.env`（`/opt/atestudio/.env`）里登录功能**依赖**这两项，缺了不会在启动时报错，
而是等到第一次登录才 500：

| 键 | 值 | 缺了会怎样 |
|---|---|---|
| `JWT_SECRET` | 随机串（HS256） | 登录/注册 **500** `TokenError: JWT_SECRET not configured` |
| `JWT_ALGORITHM` | `HS256` | 默认是 **RS256**，此时 `JWT_SECRET` 必须是 PEM 私钥；填普通随机串会在签 token 时抛异常 → 同样 **500** |

单机部署用 HS256 即可。`JWT_SECRET` 建议 `head -c 48 /dev/urandom | base64`。

> 症状与原因容易搞反：登录 500 **不是**账号密码错，是配置缺项。日志里
> `ate_cloud/auth/jwt.py` 的 `TokenError` 才是判据。

---

## 4. 五步评审流程

向导：`/aterag-review`。**规格书原件不是在界面上传的** —— 那是 ATERag 侧的事
（`ingest_document`），走完抽取用 `export_studio.py` 导出 `studio_bundle.json`，
回到第 1 步导入这个 bundle。

| 步 | 界面 | 做什么 | 卡点 |
|---|---|---|---|
| 1 | 导入抽取结果 | 粘贴/选择 bundle，先 dry-run 预览 | 需 `aterag:import` |
| 2 | 冲突处置 | 与人工修改冲突时逐条确认覆盖 | 无冲突则此步为空 |
| 3 | 条件评审 | 看需求描述、逐条签 `draft` 条件 | 需签名（`by`），无署名的批准无法审计 |
| 4 | 流程规划 | 看分段/步数/排除项/缺口 | 需 `aterag:import` |
| 5 | 批准导出 | 真落库 | **有未签字条件时被拦住**（红线） |

### 4.1 红线：未签字的条件不得作为产测判据

第 3 步签名前，所有 `status=draft` 的条件被**排除在执行序列之外**，并在规划结果里
如实列为 `pending`，同时给一条 warning 说明排除了多少条。

签字之后重新规划，这些条件才进入序列 —— 实测 `pending 62 → 60`、步数
`610 → 614`。**签字是会改变产测序列的**，不是改个状态字段。

> 踩过的坑：规划曾直接用传入的 bundle，而 ATERag 永远导出 `draft`、没有任何东西
> 重写那个文件，于是签完字 `pending` 与步数**一个都不动**，而向导自己的提示却写着
> 「评审签字后重新规划即可纳入」—— 一个结构上做不到的承诺。现在端点会把库里的
> 签字状态单向覆盖到 bundle 上，红线本身仍留在 `plan_flow` 一处不变。
> 覆盖只允许 draft→approved、只认指纹匹配且库中自身为 approved 的一行，其余一律
> 留在 draft。见 `tests/cloud/test_plan_review_overlay.py`。

### 4.2 签名依据

第 3 步每条需求上方有「需求描述(抽取合成, 非规格书原文)」面板，**不是**规格书
逐字条文 —— bundle 的 `description` 契约上写明是「输入/输出条件合成后的可读描述」。
逐条「内容」列才是原文片段（`ClauseModel.text` 按契约即「原文片段」）。核对判据请
按条款号对照规格书。

标着「人工注记 · 未经签字不得作为判据」的行，其条件来自人工注记而非规则切出。

---

## 5. 确认它真的能用

三个脚本，覆盖面**不重叠**，都通过才算数。

### 5.1 `verify_e2e.py` —— 计划与红线，不碰数据库

```bash
.venv/bin/python scripts/verify_e2e.py --bundle /tmp/studio_bundle.json
```

10 项：bundle 可读、契约 hash 与生产方一致、契约 hash 匹配冻结基准、计划非空、
**红线无未批准条件进入执行序列**、未批准条件已如实上报、无判据的测量步骤为零、
步骤 id 全局唯一、平台 DSL 解析器接受该计划、逐台稳定等待在可接受范围。

### 5.2 `verify_e2e_import.py` —— 真写 PostgreSQL

在目标板卡、部署 venv 内跑。验的是真实 PostgreSQL 而非 CI 的 SQLite —— 两者差别
恰好落在这条链路最易断的地方（批量 DDL、JSON 列、事务语义），所以 CI 通过不算证据。

> **它会 TRUNCATE `test_requirements` / `test_conditions` / `test_cases` /
> `test_limits`。** 已有 ATERag 来源数据时它**拒绝执行**并以退出码 **2** 退出：
>
> ```
> [SKIP] 该库已有 95 条 ATERag 来源需求, 拒绝覆盖
> ```
>
> 注意这个检查在**契约 hash 校验之后**才发生，所以你会先看到一条 `[PASS]` 再看到
> `[SKIP]` —— 看到 `[PASS]` 不代表它动过库，以 `[SKIP]` 和退出码为准。要在有数据的
> 板上真跑，先自己确认清库是有意为之。

### 5.3 `verify_flow_http.py` —— 走界面用的 HTTP 链路

```bash
VERIFY_USER=<用户名> VERIFY_PASSWORD=<密码> \
.venv/bin/python scripts/verify_flow_http.py \
    --base http://127.0.0.1:8000 --bundle /tmp/studio_bundle.json
```

39 项：界面页面壳与深链回退、登录、token 是否带 `aterag:import`、五步全流程、
红线是否真的在排除、签字是否真的进入序列、幂等。

**前两个脚本都查不出这一类问题**，所以第三个必须存在：

| 曾发布且全部既有检查通过的缺陷 | 前两个脚本为何看不见 |
|---|---|
| 前端构建了但没人服务，`/` 是 404 | 进程 active、健康端点 200、stamp 写 `frontend_built: true` |
| `aterag:import` 无人持有，导入 403 | 端点注册正常，路由测试 mock 了 `require_scopes` |
| 签字不改变产测序列 | 无 HTTP 层，验证脚本走的是脚本不是 API |

> 补一条：板卡库里 `users` 表曾长期为空，连登录这条路径都没被碰过。

---

## 6. 已知限制

按「会不会咬人」排。

1. **自我注册仍开放。** `POST /api/v1/auth/register` 任何人可调，建出来是 `read`。
   §2.2 的「由管理员创建用户」策略**目前没有被强制** —— 局域网里任何人都能自助
   开一个只读账号。要落实需关掉该端点或改为仅 admin 可调（首个管理员走
   `bootstrap_admin.py`）。这是删现有端点，未擅自改。
2. **降权不立即生效。** 改角色后**已签发的旧 token 仍然有效**直到过期
   （`JWT_EXPIRE_MINUTES`，默认 30 分钟）。JWT 无状态，`User` 模型里没有
   `token_version`。要即时生效需加 token 版本号。
3. **最后一个 admin 可被降权锁死。** 见 §2.3。
4. **弱默认口令 + 明文 HTTP。** 见 §1。
5. **`/rbac/roles` 与 `/rbac/permissions` 返回空。** `Role`/`Permission` 表未 seed。
   建用户不受影响（角色下拉框是前端硬编码的），但角色管理页是空的。
6. **一个真实覆盖率缺口**：`SR-PA601-D54A-1103` 的 `output_voltage` 步骤无数值
   判据，运行时只会报通过。它此前被 draft 排除顺带遮住，签字放行更多条件后才
   浮出来，并且已被如实报告。上机前须补判据或改为定性确认。
7. `tests/unit/scheduler/test_watchdog.py` 是**时序型 flaky**：同一份代码全量跑会
   时失败时通过，单独跑必过。根因是用 `sleep(0.19)` 把检查点卡在「t≈0.2 之前」。
   失败不代表产品有问题。

---

## 7. 故障速查

| 症状 | 多半是 | 怎么办 |
|---|---|---|
| 登录 500 | `JWT_SECRET` 缺失，或 `JWT_ALGORITHM` 是 RS256 而 secret 不是 PEM | 见 §3；日志搜 `TokenError` |
| 登录 401，密码确认没错 | 账号被停用（`is_active=false`） | `SELECT is_active FROM users WHERE username=...` |
| 界面 404，但服务 active | 前端没挂上 | `curl -I http://127.0.0.1:8000/`；看启动日志有无 `SPA mounted at /` || 界面 404，只有 API 路径 404 | 命中了 SPA catch-all | 未知 `/api/**` 应回 404 而非页面壳 |
| 导入/规划 403 | 账号不是 admin | `admin` 是唯一带 `aterag:import` 的角色 |
| 导入/规划 404 | 端点没注册 / 契约 hash 不匹配 | 先看 `contract_hash` |
| 第 5 步不让提交 | 还有未签字条件 | 这是红线，看 `pending_total`；去第 3 步签字 |
| 签字后步数不变 | 正常，若该条件无数值数据 | 有些 draft 条件是无数值数据的输入条件，不产生步骤 |
| `pending` 数字与库里 draft 数不一致 | 正常 | `pending` 数的是 bundle 里被排除的条件，库里还含 stale 行 |
| 删不掉某个账号 | 它是 admin | 先降级再删，见 §2.3 |

---

## 8. 相关文件

| 路径 | 作用 |
|---|---|
| `scripts/bootstrap_admin.py` | 建/提权/改密（部署期） |
| `scripts/verify_e2e.py` | 计划与红线验收（不碰库） |
| `scripts/verify_e2e_import.py` | 真库导入验收 |
| `scripts/verify_flow_http.py` | 五步流程 HTTP 走查 |
| `scripts/deploy/deploy_venv.sh` | venv 隔离部署 |
| `docs/部署手册-192.168.5.24调试服务器.md` | 开通与排错 |
| ATERag 仓库 `docs/使用说明.md` | 注记生成、抽取与导出 |
