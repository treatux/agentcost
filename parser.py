#!/usr/bin/env python3
"""
AgentCost - AI agent 用量/成本解析器
解析 Codex / Claude Code 本地日志，提取 token 用量与模型信息。
输出：按会话聚合的 JSON 记录，供仪表盘使用。
"""
import json
import glob
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---- 可配置路径（Docker 部署通过环境变量覆盖）----
CODEX_DIR = os.environ.get("AGENTCOST_CODEX_DIR", os.path.expanduser("~/.codex"))
CLAUDE_DIR = os.environ.get("AGENTCOST_CLAUDE_DIR", os.path.expanduser("~/.claude"))
HERMES_DB = os.environ.get("AGENTCOST_HERMES_DB", os.path.expanduser("~/.hermes/state.db"))
DB_DIR = os.environ.get("AGENTCOST_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USER = os.environ.get("AGENTCOST_DEFAULT_USER") or None

# ---------- 模型定价表（美元/百万token，输入/输出） ----------
# 官方基础价（rate_multiplier 的基准）
BASE_PRICES = {
    "gpt": (1.25, 10.0),
    "gpt-luna": (0.6, 4.0),
    "gpt-sol": (1.25, 10.0),
    "gpt-terra": (1.25, 10.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-opus": (15.0, 75.0),
    "deepseek": (0.56, 1.68),
    "deepseek-flash": (0.07, 0.28),
    "glm": (0.4, 1.2),
    "kimi": (0.6, 2.5),
    "qwen": (0.4, 1.6),
}

# 模型 -> family 映射（用于查 base price）
MODEL_FAMILY = {
    "gpt-5.6-terra": "gpt-terra", "gpt-5.6-sol": "gpt-sol", "gpt-5.6-luna": "gpt-luna",
    "gpt-5.5": "gpt", "gpt-5.4": "gpt", "gpt-5": "gpt", "gpt-4o": "gpt",
    "claude-sonnet-4": "claude-sonnet", "claude-sonnet-4-5": "claude-sonnet",
    "claude-opus-4": "claude-opus", "claude-opus-4-6": "claude-opus", "claude-opus-4-7": "claude-opus", "claude-opus-4-8": "claude-opus",
    "deepseek-v4-flash": "deepseek-flash", "deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "glm", "glm-5.2": "glm", "glm-4.5": "glm",
    "kimi-k2.6": "kimi", "kimi-k2": "kimi",
    "qwen3": "qwen", "qwen-max": "qwen",
}

# 上游 rate_multiplier（wanlai 实测；其他默认 1）
UPSTREAM_MULTIPLIER = {
    "wanlai": {"gpt-5.6-terra": 3, "gpt-5.6-sol": 3, "gpt-5.6-luna": 3, "gpt-5.5": 3, "gpt-5.4": 3,
               "claude-opus-4-6": 3, "claude-opus-4-7": 5, "claude-opus-4-8": 5,
               "deepseek-v4-flash": 1, "deepseek-v4-pro": 1},
}

# 上游展示名
UPSTREAM_NAMES = {
    "wanlai": "Wanlai 中转",
    "sub2api": "sub2api 自建",
    "omniroute": "OmniRoute",
    "deepseek": "DeepSeek 官方",
    "nvidia": "NVIDIA API",
    "custom": "自定义",
    "auto": "自动路由",
    "anthropic": "Anthropic 官方",
    "openai": "OpenAI 官方",
}

# 计费模式关键词：coding plan（订阅）vs API（按量）
PLAN_PROVIDERS = {"anthropic", "openai"}  # 官方订阅可能走 plan

# ---- 用户自定义模型覆盖（models_override.json）----
OVERRIDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_override.json")
_ovr = {}


def load_overrides():
    """加载用户自定义的模型上游/价格覆盖。"""
    global _ovr
    try:
        with open(OVERRIDE_PATH, encoding="utf-8") as f:
            _ovr = json.load(f)
    except Exception:
        _ovr = {}
    return _ovr


def save_overrides(overrides):
    """保存模型覆盖。"""
    global _ovr
    _ovr = overrides
    with open(OVERRIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(_ovr, f, ensure_ascii=False, indent=2)
    return _ovr


def get_override(model):
    """取某模型的用户覆盖（按完整名，再按去掉前缀的短名匹配）。"""
    if model in _ovr:
        return _ovr[model]
    short = model.split("/")[-1]
    if short in _ovr:
        return _ovr[short]
    return None


def resolve_upstream(provider, base_url="", model=""):
    """识别上游名称。优先用户覆盖，其次自动识别。"""
    if model:
        ovr = get_override(model)
        if ovr and ovr.get("upstream"):
            return str(ovr["upstream"]).lower()
    p = (provider or "").lower()
    b = (base_url or "").lower()
    if "wanlai" in p or "wanlai" in b:
        return "wanlai"
    if "sub2api" in p or "sub2api" in b:
        return "sub2api"
    if "omniroute" in p or "omniroute" in b:
        return "omniroute"
    if "nvidia" in p or "nvidia" in b:
        return "nvidia"
    if "deepseek" in p or "deepseek" in b:
        return "deepseek"
    if "anthropic" in p or "anthropic" in b or "claude.ai" in b:
        return "anthropic"
    if "openai" in p or "openai" in b:
        return "openai"
    if p and p not in ("", "auto", "custom"):
        return p.lower()
    return "custom"


def get_model_price(model, upstream):
    """按官方基础价 × 上游 multiplier 计算 (in, out) 价格。优先用户覆盖。"""
    ovr = get_override(model)
    if ovr:
        pi = ovr.get("price_in")
        po = ovr.get("price_out")
        if pi is not None and po is not None:
            return float(pi), float(po)
    fam = MODEL_FAMILY.get(model) or MODEL_FAMILY.get(model.split("/")[-1])
    if not fam:
        return None, None
    base = BASE_PRICES.get(fam)
    if not base:
        return None, None
    mult = 1
    um = UPSTREAM_MULTIPLIER.get(upstream, {})
    if um:
        mult = um.get(model, 1)
    return round(base[0] * mult, 3), round(base[1] * mult, 3)


def get_billing_mode(model, provider, upstream):
    """判断计费模式：coding_plan（订阅） / api（按量）。用户覆盖优先。"""
    ovr = get_override(model)
    if ovr and ovr.get("billing_mode"):
        return ovr["billing_mode"]
    p = (provider or "").lower()
    if upstream in PLAN_PROVIDERS:
        # 官方 provider 有订阅可能；暂标记 plan（可从配置细化）
        return "coding_plan"
    return "api"


def get_cache_prices(model, price_in):
    """获取该模型的缓存读/缓存写价格（美元/百万token）。

    优先用户覆盖（models_override.json 的 cache_price_in/cache_price_write），
    否则按行业通用系数推算：缓存读 = 输入价×0.1，缓存写 = 输入价×1.25。
    返回 (cache_read_price, cache_write_price)；price_in 缺失时返回 (None, None)。
    """
    if price_in is None:
        return None, None
    ovr = get_override(model)
    cache_read = ovr.get("cache_price_in") if ovr and ovr.get("cache_price_in") is not None else round(price_in * 0.1, 4)
    cache_write = ovr.get("cache_price_write") if ovr and ovr.get("cache_price_write") is not None else round(price_in * 1.25, 4)
    return float(cache_read), float(cache_write)


def parse_codex_session(path):
    """解析单个 Codex 会话 jsonl，返回用量聚合 dict 列表。"""
    records = []
    session_meta = {}
    session_model = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type", "")
                if t == "session_meta":
                    session_meta = d.get("payload", {})
                # model 出现在 world_state / response_item 等行中，全文件扫描第一个
                if session_model is None and "model" in line and t != "event_msg":
                    m = re.search(r'"model"\s*:\s*"([^"]+)"', line)
                    if m and m.group(1) != "<synthetic>":
                        session_model = m.group(1)
                if t == "event_msg":
                    p = d.get("payload", {})
                    if p.get("type") == "token_count":
                        info = p.get("info", {})
                        last_usage = info.get("last_token_usage") or {}
                        ts = d.get("timestamp", "")
                        records.append({
                            "ts": ts,
                            "usage": last_usage,
                            "model": session_model,
                            "provider": session_meta.get("model_provider", ""),
                            "base_url": "",
                            "billing_mode_raw": "",
                            "upstream": resolve_upstream(session_meta.get("model_provider", ""), "", session_model or ""),
                            "cwd": session_meta.get("cwd", ""),
                            "originator": session_meta.get("originator", ""),
                        })
    except Exception:
        return []
    return records


def parse_claude_session(path):
    """解析单个 Claude Code 会话 jsonl。"""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "assistant":
                    msg = d.get("message", {})
                    usage = msg.get("usage") or {}
                    # 跳过全 0 的 usage（synthetic/空响应）
                    if usage.get("input_tokens") or usage.get("output_tokens") or usage.get("cache_read_input_tokens") or usage.get("cache_creation_input_tokens"):
                        records.append({
                            "ts": d.get("timestamp", ""),
                            "usage": usage,
                            "model": msg.get("model", ""),
                            "cwd": d.get("cwd", ""),
                            "originator": "claude",
                        })
    except Exception:
        return []
    return records


def normalize_usage(usage):
    """统一各来源的 usage 字段名。

    Codex 日志用 cached_input_tokens / cache_write_input_tokens；
    Claude/Anthropic 用 cache_read_input_tokens / cache_creation_input_tokens；
    Hermes 的 scan_hermes() 已映射成后者。此函数把所有来源归一化到
    cache_read_input_tokens / cache_creation_input_tokens（幂等）。
    """
    if not isinstance(usage, dict):
        return usage or {}
    u = dict(usage)
    if "cached_input_tokens" in u and "cache_read_input_tokens" not in u:
        u["cache_read_input_tokens"] = u.pop("cached_input_tokens")
    if "cache_write_input_tokens" in u and "cache_creation_input_tokens" not in u:
        u["cache_creation_input_tokens"] = u.pop("cache_write_input_tokens")
    return u


def estimate_cost(model, usage, upstream="custom", billing_mode="api"):
    """根据实际价格估算美元成本。

    实际价格 = 未缓存价 + 缓存价，分项计费：
      新输入 token × 输入价
    + 缓存读 token × 缓存读价（用户覆盖优先，否则 输入价×0.1）
    + 缓存写 token × 缓存写价（用户覆盖优先，否则 输入价×1.25）
    + 输出 token × 输出价
    """
    if billing_mode == "coding_plan":
        return 0
    price_in, price_out = get_model_price(model, upstream)
    if price_in is None or price_out is None:
        return None
    cache_read_price, cache_write_price = get_cache_prices(model, price_in)
    if cache_read_price is None or cache_write_price is None:
        return None
    new_input = usage.get("input_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    output = (usage.get("output_tokens") or 0) + (usage.get("reasoning_output_tokens") or 0)
    cost = (
        new_input / 1e6 * price_in
        + cache_read / 1e6 * cache_read_price
        + cache_write / 1e6 * cache_write_price
        + output / 1e6 * price_out
    )
    return round(cost, 6)


def scan_hermes():
    """从 Hermes state.db 的 session_model_usage 表读取用量。"""
    records = []
    db_path = HERMES_DB
    if not os.path.exists(db_path):
        return records
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT model, billing_provider, billing_base_url, billing_mode,
                   input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, reasoning_tokens,
                   estimated_cost_usd, last_seen
            FROM session_model_usage
        """).fetchall()
        conn.close()
    except Exception:
        return records
    for (model, provider, base_url, billing_mode, inp, out, cache_r, cache_w, reason, cost, last_seen) in rows:
        usage = {
            "input_tokens": inp or 0,
            "output_tokens": out or 0,
            "cache_read_input_tokens": cache_r or 0,
            "cache_creation_input_tokens": cache_w or 0,
            "reasoning_output_tokens": reason or 0,
        }
        upstream = resolve_upstream(provider, base_url, model or "")
        records.append({
            "agent": "hermes",
            "ts": last_seen or "",
            "usage": usage,
            "model": model or "",
            "provider": provider or "",
            "base_url": base_url or "",
            "billing_mode_raw": billing_mode or "",
            "upstream": upstream,
            "cwd": "",
            "originator": "hermes",
            "cost_usd": cost,
        })
    return records


def scan_all():
    """扫描全部日志，返回记录列表。"""
    load_overrides()
    all_records = []

    # Codex
    for path in glob.glob(os.path.join(CODEX_DIR, "sessions/**/*.jsonl"), recursive=True):
        recs = parse_codex_session(path)
        for r in recs:
            r["agent"] = "codex"
        all_records.extend(recs)

    # Claude Code
    for path in glob.glob(os.path.join(CLAUDE_DIR, "projects/**/*.jsonl"), recursive=True):
        recs = parse_claude_session(path)
        for r in recs:
            r["agent"] = "claude"
            r["base_url"] = ""
            r["billing_mode_raw"] = ""
            r["upstream"] = resolve_upstream(r.get("provider", ""), "", r.get("model") or "")
        all_records.extend(recs)

    # Hermes
    all_records.extend(scan_hermes())

    # 计算成本（Hermes 自带成本则保留）
    for r in all_records:
        r["usage"] = normalize_usage(r.get("usage") or {})
        r["billing_mode"] = get_billing_mode(r.get("model"), r.get("provider"), r.get("upstream"))
        if r["billing_mode"] == "coding_plan":
            r["cost_usd"] = 0
        elif r.get("cost_usd") is None or r.get("cost_usd") == 0:
            r["cost_usd"] = estimate_cost(
                r.get("model"), r.get("usage") or {}, r.get("upstream", "custom"), r["billing_mode"]
            )
        r["price_in"], r["price_out"] = get_model_price(r.get("model"), r.get("upstream"))
        r["total_tokens"] = (
            (r.get("usage") or {}).get("input_tokens", 0)
            + (r.get("usage") or {}).get("output_tokens", 0)
            + (r.get("usage") or {}).get("cache_creation_input_tokens", 0)
            + (r.get("usage") or {}).get("cache_read_input_tokens", 0)
        )
    return all_records


def normalize_ts(ts):
    """统一时间戳格式为本地可读字符串。支持 ISO 字符串与 unix 秒。"""
    if not ts:
        return None
    # unix 秒时间戳（如 1785385049）
    if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit() and len(ts) == 10):
        try:
            dt = datetime.fromtimestamp(float(ts))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def build_db(records, db_path):
    """把记录写入 SQLite。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT,
            ts TEXT,
            model TEXT,
            provider TEXT,
            upstream TEXT,
            base_url TEXT,
            billing_mode TEXT,
            price_in REAL,
            price_out REAL,
            cwd TEXT,
            originator TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            source TEXT DEFAULT 'parser',
            request_id TEXT,
            user TEXT
        )
    """)
    # 兼容旧库：为已有表补充来源列；归一化历史上游名称并仅清理保留窗口外的解析器数据。
    cols = {row[1] for row in cur.execute("PRAGMA table_info(usage_records)").fetchall()}
    if "source" not in cols:
        cur.execute("ALTER TABLE usage_records ADD COLUMN source TEXT DEFAULT 'parser'")
    if "request_id" not in cols:
        cur.execute("ALTER TABLE usage_records ADD COLUMN request_id TEXT")
    if "user" not in cols:
        cur.execute("ALTER TABLE usage_records ADD COLUMN user TEXT")
    cur.execute("UPDATE usage_records SET upstream = lower(upstream) WHERE upstream IS NOT NULL")
    retention_days = int(os.environ.get("AGENTCOST_RETENTION_DAYS", "180"))
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("DELETE FROM usage_records WHERE source = 'parser' AND ts < ?", (cutoff,))
    for r in records:
        u = r.get("usage") or {}
        cur.execute("""
            INSERT INTO usage_records
            (agent, ts, model, provider, upstream, base_url, billing_mode, price_in, price_out,
             cwd, originator,
             input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             reasoning_tokens, total_tokens, cost_usd, source, user)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            r.get("agent"),
            normalize_ts(r.get("ts")),
            r.get("model"),
            r.get("provider"),
            r.get("upstream"),
            r.get("base_url", ""),
            r.get("billing_mode"),
            r.get("price_in"),
            r.get("price_out"),
            r.get("cwd"),
            r.get("originator"),
            u.get("input_tokens", 0),
            u.get("output_tokens", 0),
            u.get("cache_read_input_tokens", 0),
            u.get("cache_creation_input_tokens", 0),
            u.get("reasoning_output_tokens", 0),
            r.get("total_tokens", 0),
            r.get("cost_usd"),
            "parser",
            DEFAULT_USER,
        ))
    conn.commit()
    conn.close()


def main():
    records = scan_all()
    print(f"扫描到 {len(records)} 条用量记录")
    # 汇总
    from collections import Counter
    by_agent = Counter(r["agent"] for r in records)
    by_model = Counter(r.get("model") or "unknown" for r in records)
    total_cost = sum(r.get("cost_usd") or 0 for r in records)
    total_tokens = sum(r.get("total_tokens") or 0 for r in records)
    print(f"按 agent: {dict(by_agent)}")
    print(f"按模型: {dict(by_model)}")
    print(f"总 token: {total_tokens:,}  估算成本: ${total_cost:.4f}")
    if records:
        db_path = os.path.join(DB_DIR, "agentcost.db")
        build_db(records, db_path)
        print(f"已写入 {db_path}")
    else:
        print("未扫描到记录（检查日志路径）")


if __name__ == "__main__":
    main()
