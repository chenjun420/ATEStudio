# ATERag 规格导入向导 · 界面信息架构重构 · 知识图谱移除

日期:2026-09-30
基线 commit:`f1aca42`
状态:已评审通过,待实施

---

## 1. 目标

操作员**全程不碰命令行**,把一份新规格书变成一套已评审、已签字、可生成产测流程的判据集。改版重导与首导走同一条路。

同时清退一项从未启用的子系统(知识图谱 + FalkorDB),并把它所服务的「工位故障诊断」能力真正修通。

## 2. 已核实的事实基线

本方案的每一项安全性论证都依赖以下事实。它们是本次逐项实测得到的,不是读代码推断。**实施前若发现任何一条已不成立,须回到评审。**

| # | 事实 | 验证方式 |
|---|---|---|
| F1 | 前端构建产物从未被服务,`/` 长期 404,而进程 active、健康端点 200、stamp 写 `frontend_built: true` | 板卡 curl + 日志 |
| F2 | `require_scopes("aterag:import")` 守着导入与规划,而**无任何角色持有该 scope**,admin 亦 403 | 板卡以 admin token 实测两个端点 |
| F3 | 规划直接用传入 bundle,而 ATERag 永远导出 `draft` 且无任何东西重写该文件 ⇒ 签字对产测序列无影响(`pending 82→82`、`步 530→530`) | 板卡 A/B 实测 |
| F4 | `POST /api/v1/diagnose` 返回 503 `Missing credentials`。`.env` 中 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` **三个键存在但值长度为 0** ⇒ **不是接线 bug,是凭据从未填过**;`main.py` 的 `if settings.openai_api_key:` 判断正确,已正常走降级分支 | 板卡 awk 量值长度 + 进程内读 settings |
| F5 | `GET /api/v1/knowledge/graph` 返回 503 `Connection refused`;FalkorDB 单元 inactive、6379 无监听、`.env` 无 `FALKORDB_URL` ⇒ 依赖齐备但从未部署 | 板卡 systemctl/ss/psql |
| F6 | `fmeas` 表 **0 行**;`api/v1` 中**无 `nodes.py`**,节点/工位无 CRUD;`GET /dashboard/stations` 返回的是 NATS worker 心跳,非工位台账 | 板卡 psql + 路由清点 |
| F7 | `hybrid_fusion.py` 是**纯后端无关**的 RRF(`Σ 1/(k+rank)`),合并两份排序列表 ⇒ 图腿移除后退化为单路向量排序,功能正常 | 读源码 |
| F8 | ATERag MCP 17 个工具,**无导出 bundle 的工具**(`export_studio.py` 仅为脚本);`list_models` 已返回 `{products:{型号:domain}}` | 装饰器全量清点 |
| F9 | `FAULTORDB` 是硬依赖(`falkordb>=1.7.1`、`semantica[graph-falkordb,shacl]>=0.6.7`);被移除的是 Neo4j 而非 FalkorDB | `pyproject.toml` 注释原文 |
| F10 | 菜单按 `app_menus.required_permissions` 过滤,权限词汇是 `system:read`/`node:read` 等命名空间,而 `ROLE_SCOPES` 只发扁平 `admin/read/write/execute` ⇒ 交集为空,登录后主界面全空 | 板卡 API 实测 |
| F11 | **凭据在板上已存在,只是变量名体系不同**:ATERag `.env` 有 `LLM_API_KEY`(35 字符)、`LLM_BASE`(76)、`LLM_MODEL`(24)、`EMBED_API_KEY`(35)、`EMBED_MODEL`(22);ATEStudio 的三个 `OPENAI_*` 全为空值 | 板卡 awk 量值长度(不回显值) |
| F12 | ATEStudio 故障索引器因无 key 而**以零向量写入**(「failure indexer runs without embeddings」),故向量库里目前没有可用的真实向量;且诊断已有 `build_retrieval_only_result` 的**无 LLM 路径** | 读 `main.py` 与 `diagnosis_service` |
| F13 | **`qdrant-client>=1.12.0` 移除了 `QdrantClient.search()`**(改为 `query_points()`)。两处调用点(`hybrid_retriever`、`failure_indexer`)调的都是被移除的方法,异常被 `except Exception` 降级为 `logger.warning` 后 `return []` ⇒ **向量检索腿从未通过**。`/diagnose` 仍返回 200 与看似专业的根因,证据栏写 "No historical cases retrieved (top 0)",而同一向量手工调 Qdrant 有 0.71 的命中 | 板上 `/opt/atestudio/logs/api.log` 读到 `'QdrantClient' object has no attribute 'search'`;同向量手工 REST 查询 score=0.7078 |
| F14 | 服务 stdout/stderr 指向 **`/opt/atestudio/logs/api.log`**,不在 journald ⇒ `journalctl -u ate-cloud` 几乎无输出。排查此类问题必须看该文件 | 读 `/proc/<pid>/fd/1` 与 journald 对比 |
| F15 | 部署的嵌入模型是 `qwen3.7-text-embedding`,**实测 1024 维**(代码默认 1536 是 OpenAI `text-embedding-3-small` 的尺寸);其可用凭据在 **ATERag 的 `.env`**,变量名为 `LLM_*` / `EMBED_*`,与 ATEStudio 的 `OPENAI_*` 体系不同;且 `OPENAI_EMBEDDING_MODEL` 这个键**在板上 `.env` 里根本不存在** | 板上真实调用 embeddings 端点量维度;awk 量各键值长度 |
| F16 | 向量载荷若**只带 id 不带正文**,下游归纳环节会自行补内容: 首次端到端运行时,第二条证据引用把「工装夹具接触不良」描述成「电容 ESR 衰减与稳压器修调偏移」—— 流畅、自信、错误,正是 §9 点名的「部件错误」类 | 板上端到端诊断返回的 `evidence_citations` 与案例正文对照 |
| F17 | LangChain `OpenAIEmbeddings` 默认本地分词并**发送 token id**,该 tokenizer 属 OpenAI;对 Qwen 模型切分本就错误,且 DashScope 兼容端点直接拒收(`input must be an array of strings`) | 板上对照实验: 默认参数失败, `check_embedding_ctx_length=False` 成功 |

## 3. 通则:能力声称可用前必须有端到端实测

本项目已出现**六个「建好但从未启用」的能力**:F1 前端未挂载、F2 scope 无人授予、F4 诊断无凭据、F5 图谱依赖未部署、**F13 向量检索调用了已移除的客户端方法**、**F16 检索只带 id 导致引用靠编**。六者都通过了当时全部既有检查。

F13 与 F16 是本通则成立的最强证据: 它们**没有报错、没有 404、没有 403**。`/diagnose` 返回 200、给出置信度 0.75 的根因与三条修复步骤, 看上去完全正常 —— 唯一的症状是「检索结果恒为空」, 而恒为空看起来正好像「本来就没有相似历史」。若不是把案例真的录进去、再用同一个向量手工查一次 Qdrant, 这条腿可以再「正常」运行很久。

因此立此通则,并补三条具体化的推论:

> 任何能力在被声称可用之前,必须有**从入口到结果的端到端实测**。仅有单元测试、仅有构建成功、仅有 stamp 记录,均不构成可用证据。无法实测的能力按「暗着」处理。

推论一(响应正常不等于能力可用):必须检验**返回内容的实质**, 而非状态码。一个恒空的检索结果 + 一段自信的生成, 危害大于一次显式失败。

推论二(依赖 API 改名要主动防):测试应断言「代码调用的每个第三方客户端方法, 装的这个版本确实存在」。这类断裂没有异常可循, 只在运行到那一行时才出现, 而那一行通常被 ``except`` 兜着。已落地为 `tests/cloud/test_qdrant_client_compat.py`(AST 扫描 + 断言)。

推论三(降级要留痕且要能被问):`except Exception: return []` 这种「优雅降级」会把断裂伪装成正常。降级可以接受, 但降级状态必须能被查询到并呈现给用户 —— 这就是 §6 里 readiness 端点的由来。

该通则由 `scripts/verify_flow_http.py` 承载(本会话已新增,39 项),后续每期必须扩展它而非另建脚本。

## 4. 界面信息架构

### 4.1 顶层

顶栏 2 项(≤ 6 ✓),深度 3 层封顶 ✓。

```
[产测开发] [运行监控]      上下文 ▾   [＋导入]   用户 ▾
```

**依据**:NI TestStand 架构卡原文 *「depending on **mode**, edit, execute, and debug test sequences」* —— 以模式区分工程态与操作态;Ignition 建议内容区(categories)与工具区(utilities)分离;ISA-101 要求 Level 1 总览一屏不滚动。

### 4.2 产测开发 · 上下文:产品类型 → 型号

| 分组 | 页面 |
|---|---|
| 产品与型号 | **型号总览**(落地) · 导入新产品/型号 |
| 需求与用例 | 需求列表 · 用例列表 · 条件评审向导 · 需求追溯矩阵 |
| 流程与脚本 | 流程列表 · 流程编排 · 流程节点模板 · 脚本管理 · 工装设计调试器 |
| 工位绑定 | **工位管理** |

型号总览为 ISA-101 的一屏总览:每型号一行 —— 型号 · 类型 · 版本 · 需求/条件/用例计数 · **待签数** · 状态。

### 4.3 运行监控 · 上下文:厂区 → 工位

| 分组 | 页面 |
|---|---|
| 产线运行 | **实时看板**(落地) · 操作员面板 |
| 执行与数据 | 执行历史 · 测量数据 · 追溯查询 · 测试报告 |
| 调试 | 仿真调试控制台 |
| 工位运维 | 校准管理 · 换型成本与排产 |
| 故障智能 | 工位故障案例库 · FMEA 管理 · 诊断建议 · 反馈闭环 |

### 4.4 用户下拉

- **个人**:修改密码 · 语言 · 主题
- **系统**:系统设置 · 用户管理 · 角色与权限

### 4.5 全局术语替换

`节点` → **工位**(菜单、路由、i18n key、视图名);`节点流程绑定` → **工位管理**;**阶段 ↔ 工位** 对应。

## 5. 数据模型

| 实体 | 归属 | 说明 |
|---|---|---|
| 产品类型 `domain` | ATERag registry | **不建表** |
| 型号 `product_code` | ATERag registry | 不建表;ATEStudio 侧按 `test_requirements` 聚合状态 |
| **厂区** | ATEStudio 新建 | 两级上下文顶层,`parent_id` 预留 |
| **工位** | ATEStudio 新建 | 关联 `fixture_topologies`;`node_flow_bindings` 改称工位管理并指向它 |
| **工位故障案例** | ATEStudio 新建 | `station 1─n case ─┬ symptom / cause(S,O,D,RPN) / effect / fix + 验证状态` |

**关键决策:工位故障案例用规范化关系表,不是图。** 有图的形状不等于需要图数据库;几千行的邻接表在 Postgres 内即可满足聚合查询,真要迁图库是换读路径而非重写 schema。

产品不是可管理对象,而是导入的产物 —— 不建产品表。

## 6. 移除知识图谱与 FalkorDB

### 6.1 决策依据

DENSO + JST 实证研究(汽车压力传感器装配线,FMEA→图谱→故障溯因)与纯 RAG 的正面对照:

| 方法 | F1@20 |
|---|---|
| RAG 基线(以 FMEA-KG 条目做余弦检索 + LLM) | 0.267 |
| 纯 RGCN(无工艺感知) | 0.400 |
| 完整方法(本体引导概念化 + 工艺感知图学习) | 0.523 |

关键观察:**RAG 基线已经用了 FMEA 知识图谱当数据源,仍只有 0.267**。增益来自两处**算法**而非数据库:①领域概念化(抽成 Action/State/Component/Parameter)②工艺感知顺序打分。且其自身局限明显:最佳 0.52 仅在单产线 3 个场景上取得,需 A100 训练,并承认存在「语义相近但部件错误」的错误类型(把不存在的相机列为首位原因)。

结论:知识图谱**不进入第一期**。第一期为「数据采集 + 诊断建议」,即案例检索(CBR),由已在运行且已接线的 Qdrant 承担。图库部署改为**由数据量与具体问题触发**,不预先决定。

FMEA 表保留,转为工位故障案例库的结构模板。

### 6.2 影响面(精确扫描,已剔除 `semantically`/`semantica` 假阳性)

| 范围 | 数量 | 处置 |
|---|---|---|
| `src/services/` 纯图子系统 | 24 文件 | **整体删除**:`falkordb_graph_service` `graph_service` `graph_browse` `kg_evolution` `kg_retrieval` `kg_seed_data` `kg_seed_facts` `kg_seed_writer` `kg_seeder` `kg_pipeline/`(7) `ontology/`(7) `knowledge_extraction/`(6) `failure_evolution` |
| `src/api/v1/` | 4 文件 | **手术式**:`diagnose` 摘模块级 falkor 导入、保 Qdrant 腿;`health` 摘模块级导入与 `_get_or_create_graph_service`(**模块级,不摘则启动即炸**);`knowledge_reads` 摘 `/graph` 端点;`knowledge` 摘触发器 |
| `config.py` `main.py` | 2 文件 | 摘 `falkordb_url`/`falkordb_graph`/`falkordb_password`;摘 `main.py` 演化工厂与触发器 |
| `hybrid_fusion.py` `hybrid_retriever.py` | 2 文件 | **保留** —— RRF 纯函数,单路退化可用(F7) |
| 测试 | 18 文件 + `conftest.py` | 删图相关;`conftest.py` 摘 falkor fixture |
| 前端 | 4 文件 + 路由 + i18n | 删 `KnowledgeGraph.vue` + 其测试 + `knowledge.ts` graph 方法 + 路由 + menuLabels 映射 + 两语言 key |
| 脚本 | 4 文件 | 删 `provision_falkordb.sh`、`purge_neo4j.sh`;`deploy_cloud.sh` `smoke_live.sh` 摘引用 |
| 依赖 | 2 + lockfile | 删 `falkordb>=1.7.1`、`semantica[graph-falkordb,shacl]>=0.6.7`,重生成 `uv.lock`,清 mypy module 覆盖 |
| compose / 文档 | — | 删 falkordb 服务与 `FALKORDB_PASSWORD`;两份文档摘章节 |

### 6.3 执行顺序(错一步即启动失败)

1. 摘模块级导入(`health.py` `diagnose.py` `knowledge.py`)
2. 摘端点(`/knowledge/graph`、`/faults/seed`)
3. 摘配置字段
4. 摘 `main.py` 演化工厂与触发器
5. 删子系统文件
6. 删/改测试
7. 删依赖 + 重生成 `uv.lock`
8. 删 compose / 脚本 / 文档章节

### 6.4 验收闸门

移除期间唯一的功能判据:**`/diagnose` 在板上仍能以 Qdrant 单路返回结果**。另需:服务 active、`/api/v1/health/db` 200、`/` 200、前端全量测试通过。

## 7. 导入向导

三步 + 交接既有评审向导(评审向导不改,本会话修复零回归):

1. **型号与规格书** —— 选 `.md`;读首 500 字按 ATERag 规则预填型号 ID(可改,前后端双校验);`doc_version` 手输;规则内联展示
2. **抽取进行中** —— 轮询任务,展示管线阶段;失败给 ATERag 原始报错 + FAQ 指引;**MCP 不可达则降级显示 CLI 命令并可复制**
3. **结果预览** —— 从 bundle 算计数/未解析队列/confidence 分布 → 交接 `/aterag-review`

### 7.1 接口与跨仓契约

ATEStudio 新增:`POST /api/v1/products/spec`、`GET /api/v1/products/spec/jobs/{id}`、`GET /api/v1/models`(类型+型号+状态聚合)、`ingest_jobs` 表 + 迁移。

ATERag 新增:**一个** MCP 工具 `export_studio_bundle`(F8 确认缺失)。`ingest_document` 已有不动。spool 目录 `/opt/aterag/data/documents/incoming/`,atestudio 写、ATERag 读。

**bundle 契约不变** —— `contract_hash` 保持 `59a852c26ca277e4141c6a60ae723b20`,4 处硬钉不动。产品类型从 `list_models` 取,无需进 bundle。

### 7.2 与 D2/D6 的关系

D6 原文:「Agent 只读 MCP;写回走管理面」。新路径不违反:

1. **Agent 仍只读** —— 新增的是服务端确定性调用,不经过 LLM
2. **红线保护签字,不保护抽取** —— `ingest_document` 产 draft,不批准任何东西;能写判据库的仍只有 bundle 导入;批准仍只在评审第 3 步由人签名
3. **不变式进测试**

## 8. 红线不变式(三条,常驻)

移除期间尤其要盯 —— 拆 `diagnose` 最容易顺手把红线一起拆掉:

1. ingest 后新条件必为 `status="draft"`
2. approve 端点必填 `by`(无署名的批准无法审计)
3. plan 必排除 `pending > 0`

## 9. 故障智能的安全边界(第一期只到「建议」)

- **永不自动执行**,只产出带证据的候选
- **实体必须让用户确认** —— 依据:研究中「相机偏移」类错误、OEM 案例强制要求「防止静默选中错误部件并基于错误上下文生成答案」
- **全链路可追溯** —— 识别参数 / 确认实体 / 检索命中 / 最终结论
- **首次故障不自动处理**

## 10. 分期

| 期 | 内容 | 依赖 |
|---|---|---|
| **P0** | 诊断链路落地:①把「未配置」从 503 改成显式可读状态 ②配齐 ATEStudio 的 embedding 凭据(变量名与 ATERag 不同,需确认)③用**无 LLM 的 retrieval-only 路径**先让诊断建议可用 ④建工位故障案例库(关系表) | 无 |
| **P1** | 移除知识图谱与 FalkorDB(含 §6.4 闸门) | P0 |
| **P2** | 菜单重组 + 术语改名 + 上下文选择器 + 厂区/工位建模与迁移 | P1 |
| **P3** | ATERag 导出工具 + spool 约定 | P2(可并行) |
| **P4** | 上传端点 + 任务表 + `GET /api/v1/models` | P3 |
| **P5** | 导入向导 UI + 各视图按上下文分域 | P4 |
| **P6** | 注记起草进界面(可裁剪) | P5 |
| **P7** | e2e 扩展 + 操作手册更新 | P4–P6 |

P0/P1 提前的理由:`/diagnose` 是故障智能的地基,而移除图谱会让它第一次真正可用 —— **先让它能跑,再拆掉拖垮它的部分**。

### 10.1 P0 的两处修正

原 P0 写的是「修 `/diagnose` 接线 bug」。**这个前提是错的**(F4),已更正为凭据未填。

同时补充一条与嵌入模型有关的约束:

> **索引期与查询期的嵌入模型必须一致。** 故障索引器此前以**零向量**写入,故向量库目前没有可用向量;启用真实嵌入后需**重建索引**,否则旧记录与新查询的向量空间不匹配,检索会静默退化 —— 不报错,只是找不到。

因此 P0 的第一步不需要任何密钥:先把「未配置」显式化,并用已有的 retrieval-only 路径跑通。凭据配置与索引重建可并行推进。

## 11. 明确不做

产品实体表 · 4216 处内容页文案翻译 · 非 admin 角色→菜单权限映射(策略决定未定)· 真正的「当前产品」概念 · 自动执行修复。

## 12. 遗留与待决

- 「型号切换」第一期为**视图切换**,界面须明写「不改变工位运行中的型号」。真实切换(服务端状态 + NATS 下发 + 工位回执)单独立项
- 厂区/工位建成前,运行监控的上下文选择器无数据可选
- `PN2000-24A` fixture 是否真客户数据 —— 只有用户知道
- `domain_rules/power/rules.yaml` 的 `source` 字段仍含 `PA601` 可识别痕迹
- NATS 4222 无鉴权
