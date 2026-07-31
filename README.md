# AgentCost — AI Agent 用量与成本看板

监控 Claude Code / Codex / Hermes 等 AI agent 的 Token 消耗与费用，支持多上游价格识别、缓存统计、预算告警。

## 功能

- 📊 **用量看板**：今日/本周/本月/累计 Tokens 与成本
- 📈 **图表**：模型对比柱状图 + 每日趋势折线图（Tokens / 请求数 / 成本 可切换）
- 🏷️ **模型详情**：每个模型的上游、计费模式（API/Coding Plan）、输入/输出价、缓存率与缓存价格
- 💱 **货币切换**：人民币（默认） / 美元，汇率自动获取
- 📅 **时间范围**：自选日期，快捷近7天/近30天/本月/全部
- ⚙️ **模型价格管理**：自动识别 + 手动覆盖上游和价格（含缓存价格）
- 🚨 **预算告警**：超阈值 Webhook 通知（企业微信 / 钉钉 / Server酱 / 通用），独立运行不依赖任何外部框架
- 🔄 **自动刷新**：默认每 5 分钟自动解析新日志

## 快速开始（Docker）

```bash
git clone <repo-url> agentcost && cd agentcost
docker compose up -d --build
```

打开 http://localhost:8666 ，默认账号 `agentcost` / `Ac@2026!dash`（首次登录后可在 config.json 修改）。

### 支持的日志源（自动检测）

| 来源 | 路径（宿主机） |
|------|--------------|
| Codex | `~/.codex/sessions/**/*.jsonl` |
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Hermes | `~/.hermes/state.db`（session_model_usage 表） |

自定义路径：通过环境变量 `AGENTCOST_CODEX_DIR` / `AGENTCOST_CLAUDE_DIR` / `AGENTCOST_HERMES_DB` 指定。

## 手动部署（无 Docker）

```bash
# 依赖：Python 3.10+
pip install -r requirements.txt   # 当前仅标准库，可跳过

# 首次扫描
python3 parser.py

# 启动看板（自动刷新 + 告警）
python3 server.py
# 或后台常驻：systemd / supervisor
```

## 配置

所有配置保存在 `config.json`（也可在网页「设置」中修改）：

```json
{
  "monthly_budget": 2000,
  "budget_currency": "cny",
  "alert_threshold": 0.8,
  "webhook_enabled": false,
  "webhook_type": "generic",
  "webhook_url": "",
  "notify_cooldown_hours": 12
}
```

模型价格覆盖保存在 `models_override.json`（网页「模型价格管理」中修改）。

## Webhook 类型

| 类型 | 说明 |
|------|------|
| `wecom` | 企业微信机器人 Webhook |
| `dingtalk` | 钉钉机器人 Webhook |
| `serverchan` | Server酱 SendKey |
| `generic` | 通用 JSON 机器人 |

## 项目结构

```
agentcost/
├── parser.py          # 日志解析（Codex/Claude/Hermes）
├── server.py          # Web 服务 + 自动刷新 + 告警
├── check.py           # CLI 刷新/检查脚本
├── static/index.html  # 前端
├── config.json        # 配置
├── models_override.json  # 模型价格覆盖
├── agentcost.db       # SQLite 数据（自动生成）
└── Dockerfile / docker-compose.yml
```

## 安全

- 登录采用应用层 Token 认证（默认 12 小时有效）
- 生产部署建议置于 nginx 反向代理后启用 HTTPS
