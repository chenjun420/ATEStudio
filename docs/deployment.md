# ATE Studio 部署指南

## 1. 部署架构总览

ATE Studio 采用云边协同架构。云服务器部署核心服务（NATS、Qdrant、Neo4j 和 ate-cloud API），本地终端作为测试客户端运行前端和边端执行引擎，通过虚拟设备仿真模式完成全流程测试验证。

```
+--------------------------------------------------------------------+
|           Cloud Server (192.168.5.24)                                |
|  +---------+ +---------+ +---------+ +-----------+                  |
|  |  NATS   | | Qdrant  | |  Neo4j  | |  ate-cloud|                 |
|  | :4222   | | :6333   | | :7474   | |  :8000    |                 |
|  | :8222   | |         | | :7687   | |           |                 |
|  +---------+ +---------+ +---------+ +-----------+                  |
|              Bare Metal (Physical Deployment)                       |
+----------------------+----------------------------------------------+
                       | SSH / HTTP API
+----------------------+----------------------------------------------+
|         Local Terminal (Test Client)                                 |
|  +--------------+  +--------------+  +-----------+                   |
|  |  Frontend    |  |  ate-platform|  | Examples  |                   |
|  |  (Vue 3)     |  |  (Edge Worker)|  |  Scripts  |                   |
|  |  :5173       |  |  (JetStream)  |  |  (Mock)   |                   |
|  +--------------+  +--------------+  +-----------+                   |
|         Virtual Device Simulation Mode                               |
+---------------------------------------------------------------------+
```

> **部署方式说明：** 云服务器采用**物理部署（Bare Metal）**方式，所有服务直接运行在操作系统上，不使用 Docker/Podman 容器。本地开发环境仍可使用 Docker Compose（`dev` profile）进行全栈开发。

## 2. 云侧部署（物理部署）

### 2.1 服务器信息

| 项目 | 值 |
|------|-----|
| 服务器地址 | 192.168.5.24 |
| SSH 用户 | rpdzkj |
| SSH 密码 | *(通过 SSH 密钥认证，见下文)* |
| SSH 端口 | 22 |
| 操作系统 | Debian 12 (bookworm) aarch64 |
| 内存 | 15GB |
| 部署方式 | 物理部署（Bare Metal） |

### 2.2 SSH 连接

```bash
ssh -i ~/.ssh/id_rsa_atestudio rpdzkj@192.168.5.24
```

> 推荐使用 SSH 密钥认证。如需密码登录，请联系管理员获取。

### 2.3 项目传输到服务器

**方法一：Git 克隆（推荐，仓库可访问时）**

```bash
ssh rpdzkj@192.168.5.24
git clone <仓库地址> ~/ATEStudio
cd ~/ATEStudio
```

**方法二：tar + SSH 管道传输（跨平台兼容性好）**

```bash
# 在项目根目录执行（Windows PowerShell / Linux 均可）
tar czf - --exclude='.git' --exclude='.venv' --exclude='node_modules' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='data' \
  --exclude='.codegraph' --exclude='.omo' --exclude='.opencode' \
  -C src src | ssh rpdzkj@192.168.5.24 "mkdir -p ~/ATEStudio && tar xzf - -C ~/ATEStudio"
```

**方法三：rsync 增量同步（更新时效率最高）**

```bash
rsync -avz -e "ssh -p 22" --exclude='.git' --exclude='data/' --exclude='frontend/node_modules' \
  --exclude='.venv' --exclude='__pycache__' \
  ./ rpdzkj@192.168.5.24:~/ATEStudio/
```

### 2.4 安装系统依赖

云服务器需安装以下组件。所有命令通过 SSH 在服务器上执行。

#### 2.4.1 Python 3.12 + uv

```bash
# 安装 uv（Python 包管理器，会自动管理 Python 版本）
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env

# 安装 Python 3.12
uv python install 3.12
```

#### 2.4.2 NATS Server

```bash
# 下载 NATS Server（aarch64）
# 如果 GitHub 下载慢，使用镜像代理
wget -O /tmp/nats-server.tar.gz "https://gh-proxy.com/https://github.com/nats-io/nats-server/releases/download/v2.10.20/nats-server-v2.10.20-linux-arm64.tar.gz"
sudo tar -xzf /tmp/nats-server.tar.gz -C /usr/local/bin nats-server
sudo chmod +x /usr/local/bin/nats-server

# 验证
nats-server --version
```

#### 2.4.3 Qdrant

```bash
# 下载 Qdrant（aarch64 musl 变体）
# 注意：aarch64 使用 musl 变体，gnu 变体可能 404
wget -O /tmp/qdrant.tar.gz "https://gh-proxy.com/https://github.com/qdrant/qdrant/releases/download/v1.12.4/qdrant-aarch64-unknown-linux-musl.tar.gz"
sudo tar -xzf /tmp/qdrant.tar.gz -C /usr/local/bin qdrant
sudo chmod +x /usr/local/bin/qdrant

# 创建数据目录
mkdir -p ~/qdrant_data

# 验证
qdrant --version
```

#### 2.4.4 Java 21 + Neo4j

```bash
# 安装 Eclipse Temurin JDK 21（Neo4j 2025+ 需要 Java 21+）
sudo apt-get update
sudo apt-get install -y wget gnupg

# 添加 Adoptium 仓库
wget -O - https://packages.adoptium.net/artifactory/api/gpg/key/public | sudo gpg --batch --yes --import
sudo gpg --batch --yes --export --output /usr/share/keyrings/adoptium.gpg
echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/adoptium.list

# 添加 Neo4j 仓库
wget -O - https://debian.neo4j.com/neotechnology.gpg.key | sudo gpg --batch --yes --import
sudo gpg --batch --yes --export --output /usr/share/keyrings/neo4j.gpg
echo "deb [signed-by=/usr/share/keyrings/neo4j.gpg] https://debian.neo4j.com stable latest" | sudo tee /etc/apt/sources.list.d/neo4j.list

sudo apt-get update
sudo apt-get install -y temurin-21-jre neo4j
```

### 2.5 配置 Neo4j

```bash
# 编辑 Neo4j 配置
sudo tee -a /etc/neo4j/neo4j.conf > /dev/null << 'EOF'
server.default_listen_address=0.0.0.0
server.bolt.listen_address=:7687
server.http.listen_address=:7474
EOF

# 设置初始密码（请替换为安全密码）
sudo neo4j-admin dbms set-initial-password <your-neo4j-password>

# 启动 Neo4j（systemd 服务）
sudo systemctl enable neo4j
sudo systemctl start neo4j
```

### 2.6 环境配置

```bash
cd ~/ATEStudio
cp env.template .env
```

编辑 `.env` 文件，参照以下物理部署配置（注意使用 `127.0.0.1` 而非容器名）：

```env
# 数据库
ATE_CLOUD_DATABASE_TYPE=sqlite
ATE_CLOUD_SQLITE_DB_PATH=/home/rpdzkj/ATEStudio/data/ate_platform.db

# NATS 消息总线（本机地址）
ATE_CLOUD_NATS_URL=nats://127.0.0.1:4222

# Qdrant 向量数据库（本机地址）
ATE_CLOUD_QDRANT_URL=http://127.0.0.1:6333
ATE_CLOUD_EMBEDDING_DIMENSIONS=1536

# LLM 配置（阿里云 DashScope 示例）
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_BASE_URL=https://llm-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen-plus
OPENAI_EMBEDDING_MODEL=qwen3.7-text-embedding

# 仿真模式（无需物理仪器）
ATE_SIMULATION_MODE=true

# Neo4j 图数据库
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_PASSWORD=<your-neo4j-password>

# JWT 认证
JWT_SECRET=<your-jwt-secret>
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=30
```

### 2.7 安装 Python 依赖并执行数据库迁移

```bash
cd ~/ATEStudio
source ~/.local/bin/env

# 创建虚拟环境并安装依赖
uv venv --python 3.12 .venv
uv sync --no-dev

# 执行数据库迁移
source .venv/bin/activate
PYTHONPATH=src alembic upgrade head
```

### 2.8 启动云服务

按顺序启动各服务：

```bash
# 1. 启动 NATS Server（JetStream 模式）
nohup nats-server -js -m 8222 -p 4222 > /tmp/nats.log 2>&1 &

# 2. 启动 Qdrant
QDRANT__STORAGE__STORAGE_PATH=$HOME/qdrant_data nohup qdrant > /tmp/qdrant.log 2>&1 &

# 3. Neo4j 已通过 systemd 启动（见 2.5）

# 4. 启动 ate-cloud API
cd ~/ATEStudio
source ~/.local/bin/env
source .venv/bin/activate
PYTHONPATH=src nohup python -m uvicorn ate_cloud.main:app --host 0.0.0.0 --port 8000 > /tmp/ate-cloud.log 2>&1 &
```

### 2.9 云服务列表

| 服务 | 端口 | 用途 | 启动方式 |
|------|------|------|----------|
| nats | 4222, 8222 | 消息队列 + 监控面板 | nohup |
| qdrant | 6333 | 向量数据库（故障索引、RAG 检索） | nohup |
| neo4j | 7474, 7687 | 图数据库（FMEA 故障知识图谱） | systemd |
| ate-cloud | 8000 | FastAPI 云服务（API、SSE 推送、脚本版本管理） | nohup |

### 2.10 服务验证

```bash
# API 健康检查
curl http://192.168.5.24:8000/api/v1/health/db
# 期望: {"status":"healthy"}

# API 交互文档（浏览器打开）
# http://192.168.5.24:8000/docs

# NATS 监控
curl http://192.168.5.24:8222/healthz
# 期望: {"status":"ok"}

# Qdrant 健康检查
curl http://192.168.5.24:6333/healthz
# 期望: healthz check passed

# Neo4j 浏览器（浏览器打开，使用你设置的密码）
# http://192.168.5.24:7474
```

### 2.11 防火墙配置

如果服务器开启了防火墙，需开放以下端口：

```bash
# UFW 示例
sudo ufw allow 8000/tcp   # ate-cloud API
sudo ufw allow 4222/tcp   # NATS 消息总线
sudo ufw allow 8222/tcp   # NATS 监控
sudo ufw allow 6333/tcp   # Qdrant 向量数据库
sudo ufw allow 7474/tcp   # Neo4j 浏览器
sudo ufw allow 7687/tcp   # Neo4j Bolt 协议
```

### 2.12 停止与更新服务

```bash
# 停止 ate-cloud
pkill -f "uvicorn ate_cloud.main:app"

# 停止 NATS
pkill -f "nats-server"

# 停止 Qdrant
pkill -f "qdrant"

# 停止 Neo4j
sudo systemctl stop neo4j

# 更新代码后重启
cd ~/ATEStudio
# 同步最新代码（rsync 或 git pull）
source ~/.local/bin/env && source .venv/bin/activate
uv sync --no-dev
PYTHONPATH=src alembic upgrade head

# 重新启动所有服务（见 2.8）
```

## 3. 本地终端客户端部署

### 3.1 前置依赖

- Python 3.12+
- uv（Python 包管理器）
- Node.js 18+ 和 npm
- Git

### 3.2 克隆与安装

```bash
git clone <仓库地址> ATEStudio
cd ATEStudio

# 安装后端依赖
uv sync

# 安装前端依赖
cd frontend
npm install
cd ..
```

### 3.3 配置本地环境

创建本地 `.env` 文件，指向云服务器：

```env
# 云服务连接地址
ATE_CLOUD_NATS_URL=nats://192.168.5.24:4222
ATE_CLOUD_QDRANT_URL=http://192.168.5.24:6333
NEO4J_URL=bolt://192.168.5.24:7687
NEO4J_PASSWORD=<your-neo4j-password>

# 本地 SQLite 作为边端缓存
ATE_CLOUD_DATABASE_TYPE=sqlite
ATE_CLOUD_SQLITE_DB_PATH=data/ate_platform.db

# 仿真模式（虚拟设备）
ATE_SIMULATION_MODE=true
ATE_DEV_MODE=true
```

### 3.4 前端 API 代理配置

前端 `vite.config.ts` 默认将 `/api` 代理到 `http://localhost:8000`。连接远程云服务器有两种方式：

**方式 A：SSH 隧道（推荐）**

```bash
# 创建 SSH 隧道，将本地端口转发到云服务器
ssh -L 8000:localhost:8000 -L 4222:localhost:4222 rpdzkj@192.168.5.24 -p 22
# 保持此终端打开。此时 localhost:8000 将映射到云服务器的 8000 端口
```

然后在另一个终端中正常启动前端：

```bash
cd frontend
npm run dev   # 浏览器访问 http://localhost:5173
```

**方式 B：直接修改代理目标地址**

编辑 `frontend/vite.config.ts`：

```typescript
server: {
  port: 5173,
  proxy: {
    '/api': {
      target: 'http://192.168.5.24:8000',
      changeOrigin: true
    }
  }
}
```

### 3.5 运行本地边端执行引擎（可选）

如需在本地运行 JetStream 边端 Worker：

```bash
# 设置 NATS 地址指向云服务器
export ATE_CLOUD_NATS_URL=nats://192.168.5.24:4222
export ATE_SIMULATION_MODE=true

# 运行边端执行引擎
uv run python -c "
import asyncio
from ate_platform.scheduler.jetstream_worker import JetStreamWorker

async def main():
    worker = JetStreamWorker(nats_url='nats://192.168.5.24:4222')
    await worker.start()
    print(f'JetStreamWorker {worker.worker_id} started')
    while True:
        await worker.pull_and_process_one(timeout=5.0)

asyncio.run(main())
"
```

## 4. 虚拟设备仿真

### 4.1 概述

ATE Studio 提供三层仿真系统，无需连接物理测试仪器即可完成完整的测试流程验证：

| 层级 | 名称 | 作用 | 适用场景 |
|------|------|------|----------|
| Tier 1 | 驱动级 | MockDriverFactory 返回含物理感知噪声的仿真仪器读数 | 验证测试脚本逻辑，无需硬件 |
| Tier 2 | DryRun | DryRunScheduler 遍历调度图但不执行实际测项 | 验证测试序列流程和依赖关系 |
| Tier 3 | 全链路 | FullChainSimulator 组合 Tier 1 和 Tier 2 | 端到端仿真，含噪声注入的测量值 |

### 4.2 启用仿真模式

**方法一：环境变量（最简单）**

```bash
export ATE_SIMULATION_MODE=true
# 所有仪器驱动自动使用 MockDriverFactory，无需 PyVISA
```

**方法二：API 调用（按执行粒度控制）**

```bash
# 启动一次仿真执行
curl -X POST http://192.168.5.24:8000/api/v1/executions/run-001/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "tier": "full",
    "noise_model": "gaussian",
    "noise_sigma": 0.01,
    "drift_rate": 0.001,
    "bias": 0.0,
    "seed": 42
  }'
```

**方法三：前端 UI**

前端提供仿真控制面板，支持三种模式选择：
- 驱动级（Driver Level）
- DryRun
- 全链路（Full Chain）

### 4.3 运行示例测试脚本

```bash
# 使用 MockDMM 执行电压测试
python -m examples.run_test voltage_test

# 使用 MockDMM 执行电流测试
python -m examples.run_test current_test

# 使用 MockPSU 执行上电测试
python -m examples.run_test power_on_test

# 使用真实硬件（不推荐用于仿真测试）
python -m examples.run_test voltage_test --real
```

### 4.4 仿真仪器驱动列表

| 驱动 | 仿真仪器 | 模拟值范围 |
|------|----------|-----------|
| MockDMMDriver | 数字万用表 | 电压: 3.3V/5V/12V/24V ±噪声，电流: 0.1-2A，电阻: 100Ω-10kΩ |
| MockPSUDriver | 电源供应器 | 三通道，跟踪电压/电流/输出状态 |

### 4.5 编程式仿真

```python
from ate_platform.simulation.full_chain_simulator import FullChainSimulator, NoiseConfig

# 配置噪声参数
noise_config = NoiseConfig(
    noise_model="gaussian",
    noise_sigma=0.01,
    drift_rate=0.001,
    bias=0.0,
    seed=42,   # 固定种子确保结果可复现
)

# 运行全链路仿真
simulator = FullChainSimulator(noise_config=noise_config)
results = simulator.run(plan)   # plan 为 YAML 格式的测试序列

for step_id, result in results.items():
    print(f"{step_id}: {result.status}")
```

## 5. NATS Leafnode 边缘节点配置

边端 Worker 需在本地运行 NATS Leafnode 以支持离线自治运行和云边断线重连：

```bash
# 配置文件路径：config/nats-leafnode.conf
# - 本地 JetStream 存储目录：/data/jetstream-leaf（256MB 上限）
# - Leafnode 远程连接至云服务器 NATS（192.168.5.24:4222）
# - 标签：[edge, station]

# 启动本地 Leafnode
nats-server -c config/nats-leafnode.conf
```

Leafnode 的工作机制：
1. 网络正常时，消息通过 Leafnode 同步到云服务器
2. 网络断开时，本地 Worker 继续运行，消息缓冲在本地 JetStream
3. 网络恢复后，缓冲消息自动同步到云服务器

## 6. 服务资源需求

| 服务 | 内存 | CPU | 说明 |
|------|------|-----|------|
| nats | 256MB | 0.5 | JetStream 持久化存储 |
| qdrant | 512MB | 1.0 | 向量索引 |
| neo4j | 1GB | 1.0 | 图数据库 + APOC 插件 |
| ate-cloud | 512MB | 1.0 | FastAPI 应用 |
| **云侧总计** | **~2.3GB** | **~3.5** | 最小云部署配置 |

## 7. 故障排查

### 7.1 无法连接云 API

```bash
# 检查 ate-cloud 进程是否运行
ssh rpdzkj@192.168.5.24 "ps aux | grep uvicorn | grep -v grep"

# 测试健康检查接口
curl http://192.168.5.24:8000/api/v1/health/db

# 查看 ate-cloud 日志
ssh rpdzkj@192.168.5.24 "tail -50 /tmp/ate-cloud.log"
```

### 7.2 数据库迁移错误

```bash
ssh rpdzkj@192.168.5.24
cd ~/ATEStudio
source ~/.local/bin/env && source .venv/bin/activate

# 查看当前迁移版本
PYTHONPATH=src alembic current

# 执行迁移
PYTHONPATH=src alembic upgrade head

# 查看迁移历史
PYTHONPATH=src alembic history
```

### 7.3 NATS 连接失败

```bash
# 检查 NATS 健康状态
curl http://192.168.5.24:8222/healthz

# 检查 NATS 进程
ssh rpdzkj@192.168.5.24 "ps aux | grep nats-server | grep -v grep"

# 查看 NATS 日志
ssh rpdzkj@192.168.5.24 "tail -30 /tmp/nats.log"

# 创建 SSH 隧道后测试 NATS 连通性
ssh -L 4222:localhost:4222 rpdzkj@192.168.5.24 -p 22
```

### 7.4 Neo4j 连接问题

```bash
# 检查 Neo4j 服务状态
ssh rpdzkj@192.168.5.24 "sudo systemctl status neo4j"

# 测试 Neo4j 连通性
cypher-shell -u neo4j -p <your-neo4j-password> -a bolt://192.168.5.24:7687 "RETURN 1"

# 查看 Neo4j 日志
ssh rpdzkj@192.168.5.24 "sudo journalctl -u neo4j --tail 50"
```

### 7.5 Qdrant 连接问题

```bash
# 检查 Qdrant 进程
ssh rpdzkj@192.168.5.24 "ps aux | grep qdrant | grep -v grep"

# 测试 Qdrant 健康检查
curl http://192.168.5.24:6333/healthz

# 查看 Qdrant 日志
ssh rpdzkj@192.168.5.24 "tail -30 /tmp/qdrant.log"
```

### 7.6 GitHub 下载慢的解决方案

在中国大陆网络环境中，GitHub 下载可能很慢。使用镜像代理：

```bash
# 使用 gh-proxy.com 镜像
wget -O /tmp/nats-server.tar.gz "https://gh-proxy.com/https://github.com/nats-io/nats-server/releases/download/v2.10.20/nats-server-v2.10.20-linux-arm64.tar.gz"

# 或使用 ghfast.top 镜像
wget -O /tmp/qdrant.tar.gz "https://ghfast.top/https://github.com/qdrant/qdrant/releases/download/v1.12.4/qdrant-aarch64-unknown-linux-musl.tar.gz"
```

### 7.7 服务自启动（可选）

如需服务器重启后自动启动服务，可创建 systemd 服务：

```bash
# NATS systemd 服务
sudo tee /etc/systemd/system/nats.service > /dev/null << 'EOF'
[Unit]
Description=NATS Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/nats-server -js -m 8222 -p 4222
Restart=always
User=rpdzkj

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable nats
sudo systemctl start nats

# Qdrant systemd 服务
sudo tee /etc/systemd/system/qdrant.service > /dev/null << 'EOF'
[Unit]
Description=Qdrant Vector Database
After=network.target

[Service]
Type=simple
Environment=QDRANT__STORAGE__STORAGE_PATH=/home/rpdzkj/qdrant_data
ExecStart=/usr/local/bin/qdrant
Restart=always
User=rpdzkj

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable qdrant
sudo systemctl start qdrant

# ate-cloud systemd 服务
sudo tee /etc/systemd/system/ate-cloud.service > /dev/null << 'EOF'
[Unit]
Description=ATE Cloud API
After=network.target nats.service qdrant.service

[Service]
Type=simple
WorkingDirectory=/home/rpdzkj/ATEStudio
Environment=PYTHONPATH=src
ExecStart=/home/rpdzkj/ATEStudio/.venv/bin/python -m uvicorn ate_cloud.main:app --host 0.0.0.0 --port 8000
Restart=always
User=rpdzkj

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable ate-cloud
sudo systemctl start ate-cloud
```

## 8. 本地开发环境（Docker）

本地开发仍可使用 Docker Compose 进行全栈开发：

```bash
# 全栈开发（包含 ate-platform 边端引擎）
docker compose --profile dev up -d

# 仅云服务
docker compose --profile cloud up -d

# 执行数据库迁移
docker exec -it ate-studio-ate-cloud-1 alembic upgrade head
```

> 注意：云服务器（192.168.5.24）使用物理部署，不使用 Docker。Docker 方式仅用于本地开发环境。
