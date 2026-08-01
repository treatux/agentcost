# AgentCost — AI Agent 用量与成本看板

AgentCost 是一个零第三方 Python 依赖的本地看板：扫描 Codex、Claude Code、Hermes 日志或接收 HTTP 数据，集中查看 AI Agent 的 Token 用量和成本。服务默认监听 `8666` 端口。

## 功能清单

- 登录认证、日期范围筛选与汇总卡片
- 模型对比、每日用量趋势和缓存节省金额
- 成本分布环形图、月底成本预测与环比
- 月度预算进度条，以及 Webhook 预算告警
- CSV 导出、HTTP 数据接入 API 和自动刷新
- 移动端适配、人民币/美元切换与模型价格覆盖

## 快速开始

### 本地运行

仅需 Python 3.10+，不需要安装任何包：

```bash
cd /root/agentcost
python3 parser.py       # 首次扫描本机日志（可选）
python3 server.py
```

浏览器打开 <http://localhost:8666>。默认账号为 `agentcost`，密码为 `Ac@2026!dash`。

健康检查无需登录：

```bash
curl http://localhost:8666/api/health
```

### systemd

已有服务可保持以下启动命令：

```ini
[Service]
WorkingDirectory=/root/agentcost
ExecStart=/usr/bin/python3 /root/agentcost/server.py
Restart=always
Environment=AGENTCOST_DATA_DIR=/root/agentcost/data
```

修改 unit 后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agentcost
sudo systemctl status agentcost
```

### Docker

```bash
cd /root/agentcost
docker compose up -d --build
```

`docker-compose.yml` 将 `./data` 挂载到容器 `/data`，用来持久化 SQLite 数据库与网页设置。日志目录也以只读方式挂载；若日志不在默认位置，可调整 compose 中的环境变量和卷挂载。

## 配置说明

所有环境变量均为可选：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AGENTCOST_PORT` | `8666` | HTTP 服务端口 |
| `AGENTCOST_DATA_DIR` | 项目目录 | `agentcost.db`、`config.json`、`models_override.json` 的存放目录 |
| `AGENTCOST_CODEX_DIR` | `~/.codex` | Codex 日志根目录 |
| `AGENTCOST_CLAUDE_DIR` | `~/.claude` | Claude Code 日志根目录 |
| `AGENTCOST_HERMES_DB` | `~/.hermes/state.db` | Hermes SQLite 状态库路径 |
| `AGENTCOST_REFRESH_SECONDS` | `300` | 后台自动重扫日志的间隔（秒） |

解析器与 Web 服务共用 `AGENTCOST_DATA_DIR`；因此在 Docker 或 systemd 中设置一次即可。模型价格覆盖由设置面板保存到 `models_override.json`。

## 数据接入 API

向 `POST /api/ingest` 发送单条记录或 `records` 数组。认证二选一：登录后获得的 `X-Token`，或设置面板生成/设置的独立 `X-Ingest-Key`。

```bash
curl -X POST http://localhost:8666/api/ingest \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Key: your-ingest-key' \
  -d '{
    "agent": "custom-agent",
    "model": "gpt-5.6",
    "input_tokens": 1200,
    "output_tokens": 340,
    "cost_usd": 0.012
  }'
```

也可以使用登录 Token：

```bash
curl -X POST http://localhost:8666/api/ingest \
  -H 'Content-Type: application/json' \
  -H 'X-Token: <login-token>' \
  -d '{"agent":"custom-agent","model":"claude-sonnet-4","total_tokens":3000}'
```

## 设置面板

登录后在网页设置中可配置：月度预算和币种、告警阈值、Webhook 类型/地址、独立数据接入 Key，以及模型上游和输入/输出/缓存价格覆盖。Webhook 支持企业微信、钉钉、Server 酱和通用 JSON Webhook。

## 截图

> 截图占位：可在此放置总览看板、模型对比和设置面板的截图，例如 `docs/dashboard.png`。

## 安全提示

应用层 Token 默认有效期为 12 小时。生产环境建议放在 HTTPS 反向代理之后，不要将默认账号密码直接暴露到公网。
