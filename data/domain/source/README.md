# ATEStudio 部署初始化通用知识（data/domain/source · wave 12.5）

> 本目录作为 ATEStudio 仓库内「部署初始化数据」的载体，与
> MATRag `data/domain/source/` 的 canonical 文件**逐位一致**（同文件、同 sha256），
> 让 ATEStudio 在网络隔离 / cold-start / 离线 CI 场景下也能自洽加载领域知识。

## 包含的文件（来自 MATRag canonical）

| 文件 | 用途 |
|---|---|
| `power_terms.yaml`     | 术语词汇表（M-* 方法 ID、PCI/PSU/... 缩写、知识图谱节点） |
| `power_axioms.yaml`    | 领域公理（电源产测基准的不变量与定律性条款） |
| `power_rules.yaml`     | 规则库（K-* 知识 ID + 规则体） |
| `power_knowledge.md`   | 结构化 markdown 摘要（知识图谱行业背景） |
| `power_test_methods.yaml` | M-* 测试方法（35 条；含 wave-11 新增 STARTUP-TIME/OVERSHOOT/CROSS-REG） |
| `method_ref_aliases.yaml` | M-* ↔ 业界别名映射（aliases v4 = 25 条） |
| `instrument_catalog.yaml` | 仪器 catalog（28 仪器 ID：chroma / keysight / tek / fluke / ...） |

## 同步策略

- 仓库内**只读** snapshot；不修改这些 yaml。
- 双向同步来源单一：MATRag `data/domain/source/` 是 canonical，ATEStudio 这份是「部署初始化副本」。
- 同步方法（任选其一）：
  1. CI 在 `publish-domain-snapshot` job 中 cp + `git add` + 自动 PR；
  2. 现场使用 `git submodule add` 把 MATRag 该目录挂到 `data/domain/source`（更 clean，但增加 .gitmodules 维护成本）。

## 引用此目录的代码（ATEStudio 侧）

- `ate_platform/services/flow_planner.py` / `*cond_fingerprint.py` 等按需 load；
- 离线 dry-run 已统一改用此副本而非 HTTP 拉 MATRag（避免跨仓依赖）。

## 数据规模

- 7 个文件，约 466 KB（与 MATRag 端一致）。
