#!/usr/bin/env python3
"""
AgentCost - 仪表盘后端 v2
- 自定义登录页（应用层 token 认证，不用 Basic Auth）
- 日期范围筛选（from/to 参数）
运行: python3 server.py  →  http://localhost:8666
"""
import json
import os
import sys
import time
import csv
import io
import sqlite3
import secrets
import hashlib
import hmac
import subprocess
import threading
import urllib.request
import shutil
import parser as cost_parser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

__version__ = "1.3.0"
START_TIME = time.time()
LAST_SCAN_AT = None

DB_DIR = os.environ.get("AGENTCOST_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "agentcost.db")
PARSER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parser.py")
# 将可变配置与数据库放在同一个数据目录，容器挂载 /data 后可持久化。
OVERRIDE_PATH = os.path.join(DB_DIR, "models_override.json")
CONFIG_PATH = os.path.join(DB_DIR, "config.json")
USERS_PATH = os.path.join(DB_DIR, "users.json")
PORT = int(os.environ.get("AGENTCOST_PORT", "8666"))
HOST = os.environ.get("AGENTCOST_HOST", "127.0.0.1")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
AUTO_REFRESH_SECONDS = int(os.environ.get("AGENTCOST_REFRESH_SECONDS", "300"))  # 默认每 5 分钟自动刷新
_last_price_fetch_date = None
_last_backup_date = None
_backup_lock = threading.Lock()


def get_conn():
    """打开适合并发读写的 SQLite 连接。"""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

# ---- 自动刷新：定时重跑 parser 更新 DB ----
def run_parser():
    global LAST_SCAN_AT
    try:
        result = subprocess.run(
            [sys.executable, PARSER_PATH],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            LAST_SCAN_AT = datetime.now().astimezone().isoformat()
            return True
        return False
    except Exception:
        return False


# ---- 配置（可被前端设置面板修改） ----
DEFAULT_CONFIG = {
    "monthly_budget": 2000.0,      # 预算金额（单位由 budget_currency 决定）
    "budget_currency": "cny",      # cny / usd
    "alert_threshold": 0.8,
    "webhook_enabled": False,
    "webhook_type": "generic",   # wecom / serverchan / dingtalk / generic
    "webhook_url": "",
    "notify_cooldown_hours": 12, # 同一告警等级 12 小时内不重复通知
    "anomaly_enabled": False,
    "anomaly_multiplier": 3.0,
    "anomaly_cooldown_hours": 24,
    "report_enabled": False,
    "report_time": "09:00",     # 每日推送时间（24 小时制）
    "report_weekday": 1,          # 周报推送日：1=周一 ... 7=周日，0=每天
    "report_type": "daily",     # daily / weekly
    "ingest_key": "",             # 独立数据接入密钥；为空时仅允许登录 token
    "share_enabled": False,
    "share_token": "",
    "share_user": "",
    "share_role": "",
    "backup_dir": os.path.join(DB_DIR, "backups"),
}
_config = dict(DEFAULT_CONFIG)
_last_notify = {}  # level -> timestamp
_last_anomaly_notify = {}  # anomaly level -> timestamp
_last_report_date = None  # YYYY-MM-DD，仅在当前进程内防止同日重复推送


def backup_dir():
    """返回配置的备份目录；相对路径以数据目录为基准。"""
    configured = str(_config.get("backup_dir") or os.path.join(DB_DIR, "backups"))
    if not os.path.isabs(configured):
        configured = os.path.join(DB_DIR, configured)
    return os.path.realpath(configured)


def list_backups():
    """列出当前备份目录中的常规文件，按最新修改时间在前。"""
    root = backup_dir()
    if not os.path.isdir(root):
        return []
    files = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            stat = os.stat(path)
            files.append({"name": name, "size": stat.st_size, "mtime": stat.st_mtime})
    return sorted(files, key=lambda item: item["mtime"], reverse=True)


def create_backup():
    """用 SQLite backup API 创建当日数据库和配置快照，并只保留最近 7 天。"""
    root = backup_dir()
    date = datetime.now().strftime("%Y%m%d")
    db_name = f"agentcost-{date}.db"
    os.makedirs(root, exist_ok=True)
    with _backup_lock:
        target = os.path.join(root, db_name)
        source = get_conn()
        destination = sqlite3.connect(target, timeout=5)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        # 首次运行尚未有 config.json 时，也让备份包含可恢复的默认配置。
        if not os.path.isfile(CONFIG_PATH):
            save_config(_config)
        # 配置快照与同日数据库放在一起。不存在的可选文件直接跳过。
        config_files = (
            (USERS_PATH, "users"),
            (CONFIG_PATH, "config"),
            (OVERRIDE_PATH, "models_override"),
            (getattr(cost_parser, "OFFICIAL_PRICES_PATH", ""), "official_prices"),
        )
        for source_path, label in config_files:
            if source_path and os.path.isfile(source_path):
                shutil.copy2(source_path, os.path.join(root, f"{label}-{date}.json"))

        # 以数据库快照日期作为一份备份的边界，连同该日期的配置文件一起删除。
        dates = sorted(
            name[len("agentcost-"):-len(".db")]
            for name in os.listdir(root)
            if name.startswith("agentcost-") and name.endswith(".db")
            and len(name) == len("agentcost-YYYYMMDD.db") and name[10:18].isdigit()
        )
        for old_date in dates[:-7]:
            for name in os.listdir(root):
                if name == f"agentcost-{old_date}.db" or name.endswith(f"-{old_date}.json"):
                    old_path = os.path.join(root, name)
                    if os.path.isfile(old_path):
                        os.remove(old_path)
    return db_name


def alert_in_cooldown(alert_type, level, now_iso):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT 1 FROM alert_history WHERE type = ? AND level = ?
               AND cooldown_until > ? ORDER BY id DESC LIMIT 1""",
            (alert_type, level, now_iso),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_alert(alert_type, level, message, cooldown_seconds):
    now = datetime.now().astimezone()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO alert_history (ts, level, type, message, cooldown_until) VALUES (?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), level, alert_type, message,
             (now + timedelta(seconds=cooldown_seconds)).isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def load_config():
    global _config
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user_cfg = json.load(f)
        _config = {**DEFAULT_CONFIG, **user_cfg}
        # 兼容旧配置：只有 monthly_budget_usd 时迁移为 CNY 预算
        if "monthly_budget_usd" in user_cfg and "monthly_budget" not in user_cfg:
            usd = float(user_cfg.get("monthly_budget_usd", 200))
            _config["monthly_budget"] = usd * 7.1
            _config["budget_currency"] = "cny"
            save_config(_config)
    except Exception:
        _config = dict(DEFAULT_CONFIG)
    return _config


def save_config(cfg):
    """合并保存配置（只保留已知字段）。"""
    global _config
    merged = dict(_config)
    for k in DEFAULT_CONFIG:
        if k in cfg:
            merged[k] = cfg[k]
    _config = merged
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_config, f, ensure_ascii=False, indent=2)
    return _config


# ---- 模型覆盖管理 ----
def load_models_override():
    try:
        with open(OVERRIDE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_models_override(ovr):
    with open(OVERRIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(ovr, f, ensure_ascii=False, indent=2)
    return ovr


def model_catalog(user=None):
    """返回所有已知模型 + 当前上游/价格 + 是否被用户覆盖。"""
    conn = get_conn()
    cur = conn.cursor()
    where = ""
    params = ()
    if user:
        where = " WHERE user = ?"
        params = (user,)
    rows = cur.execute(f"""
        SELECT model, upstream, price_in, price_out, billing_mode,
               COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost
        FROM usage_records{where} GROUP BY model, upstream ORDER BY tokens DESC
    """, params).fetchall()
    conn.close()
    ovr = load_models_override()
    models = []
    for (model, upstream, pi, po, bm, cnt, tokens, cost) in rows:
        short = model.split("/")[-1]
        entry = {
            "model": model,
            "upstream": upstream,
            "price_in": pi,
            "price_out": po,
            "billing_mode": bm,
            "count": cnt,
            "tokens": tokens,
            "cost": cost,
            "overridden": model in ovr or short in ovr,
        }
        models.append(entry)
    return {"models": models, "overrides": ovr}


def official_prices_for_local_models():
    """仅返回本地用量数据库中出现过的模型的官方价格。"""
    cached = cost_parser.load_official_prices()
    all_prices = cached.get("prices", {})
    try:
        rows = query_db("SELECT DISTINCT model FROM usage_records WHERE model IS NOT NULL AND model != ''")
    except sqlite3.Error:
        rows = []
    prices = {}
    for row in rows:
        model = row["model"]
        price = cost_parser.get_official_price(model, all_prices)
        if isinstance(price, dict):
            prices[model] = price
    return {"fetched_at": cached.get("fetched_at", ""), "prices": prices}


def send_webhook(text):
    """按配置的 webhook 类型发送通知。"""
    cfg = _config
    if not cfg.get("webhook_enabled") or not cfg.get("webhook_url"):
        return False
    url = cfg["webhook_url"]
    wtype = cfg.get("webhook_type", "generic")
    headers = {"Content-Type": "application/json"}
    try:
        if wtype == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
            data = json.dumps(payload).encode()
        elif wtype == "dingtalk":
            payload = {"msgtype": "text", "text": {"content": text}}
            data = json.dumps(payload).encode()
        elif wtype == "serverchan":
            # 兼容 sctapi.ftqq.com/{key}.send 与 turbo 版
            data = json.dumps({"title": "AgentCost 预算提醒", "desp": text}).encode()
            headers = {"Content-Type": "application/json"}
        else:  # generic：默认发送 JSON {text: ...}，兼容多数机器人
            data = json.dumps({"text": text, "content": text, "message": text}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print(f"[webhook] 发送失败: {e}")
        return False


def check_budget_and_notify():
    """刷新后检查预算，超阈值且冷却期外则发 webhook。"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        month_start = datetime.now().strftime("%Y-%m-01")
        row = cur.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM usage_records WHERE ts >= ?",
            (month_start,)
        ).fetchone()
        conn.close()
        cost_usd = row[0] or 0
        fx = fetch_fx_rate()

        # 预算换算成 USD 比较（DB 里成本以 USD 计）
        budget_amount = float(_config.get("monthly_budget", 2000) or 1)
        currency = _config.get("budget_currency", "cny")
        budget_usd = budget_amount / fx if currency == "cny" else budget_amount
        if budget_usd <= 0:
            budget_usd = 1
        pct = cost_usd / budget_usd * 100
        threshold = _config.get("alert_threshold", 0.8) * 100
        now = time.time()
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

        level = None
        if cost_usd >= budget_usd:
            level = "over"
        elif pct >= threshold:
            level = "warn"

        if level is None:
            return
        # 冷却期检查
        cooldown = _config.get("notify_cooldown_hours", 12) * 3600
        if alert_in_cooldown("budget", level, now_iso):
            return
        _last_notify[level] = now
        # 显示用：按预算货币显示金额
        if currency == "cny":
            cur_text = f"¥{cost_usd*fx:.0f} / ¥{budget_amount:.0f}"
        else:
            cur_text = f"${cost_usd:.2f} / ${budget_usd:.2f}"
        text = (f"🚨 AgentCost 预算告警（{level}）\n"
                f"本月已用: {cur_text}\n"
                f"使用率: {pct:.1f}%")
        send_webhook(text)
        # 无论 webhook 成功与否都保留本次尝试，供审计和跨重启冷却使用。
        record_alert("budget", level, text, cooldown)
    except Exception as e:
        print(f"[budget] 检查失败: {e}")


def check_anomaly_and_notify():
    """比较今日成本与最近 7 个有数据日期的日均成本并发送异常告警。"""
    if not _config.get("anomaly_enabled", False):
        return
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = get_conn()
        cur = conn.cursor()
        cost_today = cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_records WHERE ts LIKE ?",
            (today + "%",),
        ).fetchone()[0] or 0
        # 以最近 7 个（不含今天）有记录的自然日为基准，避免单日多条记录影响平均值。
        daily_rows = cur.execute(
            """SELECT substr(ts, 1, 10) AS day, COALESCE(SUM(cost_usd), 0) AS cost
               FROM usage_records
               WHERE substr(ts, 1, 10) < ?
               GROUP BY day ORDER BY day DESC LIMIT 7""",
            (today,),
        ).fetchall()
        conn.close()
        if not daily_rows:
            return
        cost_avg = sum(float(row[1] or 0) for row in daily_rows) / len(daily_rows)
        multiplier = float(_config.get("anomaly_multiplier", 3.0) or 3.0)
        if multiplier < 1.5:
            multiplier = 1.5
        if cost_avg <= 0 or cost_today <= cost_avg * multiplier:
            return

        now = time.time()
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        cooldown = float(_config.get("anomaly_cooldown_hours", 24) or 24) * 3600
        level = "warn"
        if alert_in_cooldown("anomaly", level, now_iso):
            return
        _last_anomaly_notify[level] = now

        currency = _config.get("budget_currency", "cny")
        fx = fetch_fx_rate()
        if currency == "cny":
            today_text = f"¥{cost_today * fx:.2f}（USD ${cost_today:.2f}）"
            avg_text = f"¥{cost_avg * fx:.2f}"
        else:
            today_text = f"${cost_today:.2f}"
            avg_text = f"${cost_avg:.2f}"
        text = ("🚨 AgentCost 异常检测\n"
                f"今日成本: {today_text}\n"
                f"近7天日均: {avg_text}\n"
                f"超出倍数: {cost_today / cost_avg:.1f}倍")
        send_webhook(text)
        record_alert("anomaly", level, text, cooldown)
    except Exception as e:
        print(f"[anomaly] 检查失败: {e}")


def _format_report_money(data, key="cost"):
    """按预算货币格式化汇总中的金额。"""
    usd_key = "saved_usd" if key == "saved" else key
    cny_key = "saved_cny" if key == "saved" else key + "_cny"
    if _config.get("budget_currency", "cny") == "cny":
        return f"¥{(data.get(cny_key) or 0):.2f}"
    return f"${(data.get(usd_key) or 0):.2f}"


def _format_report_tokens(tokens):
    tokens = tokens or 0
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.2f}K"
    return str(int(tokens))


def _report_top_models(data, limit=1):
    # summary 按 model + upstream 分组；报告中合并同名模型，避免 Top 3 出现重复项。
    costs = {}
    for item in data.get("by_model", []):
        model = item.get("model") or "未知模型"
        costs[model] = costs.get(model, 0) + (item.get("cost") or 0)
    return [model for model, _ in sorted(costs.items(), key=lambda item: item[1], reverse=True)[:limit]]


def build_report_text(kind):
    """生成日报或周报文本，供定时 Webhook 推送使用。"""
    if kind not in ("daily", "weekly"):
        raise ValueError("报告类型必须是 daily 或 weekly")

    today = datetime.now().date()
    last_complete_day = today - timedelta(days=1)
    if kind == "daily":
        report_day = last_complete_day
        data = summary(report_day.isoformat(), report_day.isoformat())
        # 昨天没有数据时，回退到最近一个有记录的已完成日期。
        if not data["total"].get("cnt"):
            rows = query_db(
                "SELECT MAX(substr(ts,1,10)) AS day FROM usage_records WHERE ts < ?",
                (today.isoformat(),),
            )
            if rows and rows[0].get("day"):
                report_day = datetime.strptime(rows[0]["day"], "%Y-%m-%d").date()
                data = summary(report_day.isoformat(), report_day.isoformat())

        total = data["total"]
        top_models = _report_top_models(data)
        prev_cost = data["prev"].get("cost") or 0
        cost = total.get("cost") or 0
        change = "—" if not prev_cost else f"{(cost - prev_cost) / prev_cost * 100:+.1f}%"
        return "\n".join((
            f"📊 AgentCost 日报 ({report_day.strftime('%m-%d')})",
            f"成本: {_format_report_money(total)} | Tokens: {_format_report_tokens(total.get('tokens'))} | 请求: {total.get('cnt') or 0}",
            f"缓存节省: {_format_report_money(total, 'saved')}",
            f"Top 模型: {top_models[0] if top_models else '暂无数据'}",
            f"环比: {change}",
        ))

    week_to = last_complete_day
    week_from = week_to - timedelta(days=6)
    data = summary(week_from.isoformat(), week_to.isoformat())
    total = data["total"]
    top_models = _report_top_models(data, 3)
    average_cost = (total.get("cost") or 0) / 7
    if _config.get("budget_currency", "cny") == "cny":
        average_text = f"¥{average_cost * (data.get('fx') or 0):.2f}"
    else:
        average_text = f"${average_cost:.2f}"
    budget = float(_config.get("monthly_budget", 0) or 0)
    month_used = data["forecast_monthly"].get("used") or 0
    if _config.get("budget_currency", "cny") == "cny":
        month_used *= data.get("fx") or 0
    budget_pct = month_used / budget * 100 if budget > 0 else 0
    return "\n".join((
        f"📈 AgentCost 周报 ({week_from.strftime('%m-%d')} ~ {week_to.strftime('%m-%d')})",
        f"成本: {_format_report_money(total)} | Tokens: {_format_report_tokens(total.get('tokens'))} | 请求: {total.get('cnt') or 0}",
        f"日均成本: {average_text} | 缓存节省: {_format_report_money(total, 'saved')}",
        f"预算使用率: {budget_pct:.1f}%",
        f"Top 模型: {', '.join(top_models) if top_models else '暂无数据'}",
    ))


def check_scheduled_report():
    """在现有刷新循环中检查并发送当天的日报或周报。"""
    global _last_report_date
    cfg = _config
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not cfg.get("report_enabled") or _last_report_date == today:
        return
    report_time = str(cfg.get("report_time") or "09:00")
    try:
        datetime.strptime(report_time, "%H:%M")
    except ValueError:
        print(f"[report] 无效推送时间: {report_time}")
        return
    if now.strftime("%H:%M") < report_time:
        return
    kind = cfg.get("report_type", "daily")
    if kind == "weekly":
        weekday = int(cfg.get("report_weekday", 1) or 0)
        if weekday and now.isoweekday() != weekday:
            return
    if kind not in ("daily", "weekly"):
        print(f"[report] 无效报告类型: {kind}")
        return
    _last_report_date = today
    send_webhook(build_report_text(kind))


def maybe_fetch_official_prices():
    """每天最多自动抓取一次；已有未满 24 小时的缓存直接复用。"""
    global _last_price_fetch_date
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    if _last_price_fetch_date == today:
        return False
    cached = cost_parser.load_official_prices()
    try:
        fetched_at = str(cached.get("fetched_at") or "").replace("Z", "+00:00")
        last_fetch = datetime.fromisoformat(fetched_at)
        if last_fetch.tzinfo is None:
            last_fetch = last_fetch.replace(tzinfo=now.tzinfo)
        if (now - last_fetch.astimezone(now.tzinfo)).total_seconds() < 24 * 3600:
            return False
    except (TypeError, ValueError):
        pass
    fetched, _error = cost_parser.fetch_official_prices()
    if fetched:
        _last_price_fetch_date = today
        return True
    return False


def auto_refresh_loop():
    """后台线程：启动时刷一次，之后每 5 分钟刷一次；每次刷新后检查预算。"""
    global _last_backup_date
    maybe_fetch_official_prices()
    run_parser()
    load_config()
    today = datetime.now().date().isoformat()
    if _last_backup_date != today:
        try:
            create_backup()
            _last_backup_date = today
        except Exception as e:
            print(f"[backup] 自动备份失败: {e}")
    check_budget_and_notify()
    check_anomaly_and_notify()
    while True:
        time.sleep(AUTO_REFRESH_SECONDS)
        try:
            maybe_fetch_official_prices()
            run_parser()
            today = datetime.now().date().isoformat()
            if _last_backup_date != today:
                create_backup()
                _last_backup_date = today
            check_budget_and_notify()
            check_anomaly_and_notify()
            check_scheduled_report()
        except Exception:
            pass

# ---- 汇率（USD -> CNY），启动时获取一次并缓存 ----
FX_RATE = 7.1  # 兜底汇率
FX_FETCHED_AT = None


def fetch_fx_rate():
    """获取 USD/CNY 汇率，失败时用兜底值。"""
    global FX_RATE, FX_FETCHED_AT
    now = datetime.now()
    # 缓存 6 小时
    if FX_FETCHED_AT and (now - FX_FETCHED_AT).total_seconds() < 6 * 3600:
        return FX_RATE
    for url in (
        "https://open.er-api.com/v6/latest/USD",
        "https://api.exchangerate-api.com/v4/latest/USD",
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
            rate = data.get("rates", {}).get("CNY")
            if rate:
                FX_RATE = float(rate)
                FX_FETCHED_AT = now
                break
        except Exception:
            continue
    return FX_RATE

# ---- 用户与认证 ----
def password_hash(password):
    """旧版 SHA-256 哈希，仅用于兼容无 salt 的历史 users.json。"""
    return hashlib.sha256(str(password).encode("utf-8")).hexdigest()


def password_hash_pbkdf2(password, salt=None):
    """返回 (PBKDF2-SHA256 哈希, hex salt)。"""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt_bytes, 100000)
    return digest.hex(), salt_bytes.hex()


def make_user_entry(password, role="user"):
    digest, salt = password_hash_pbkdf2(password)
    return {"password_hash": digest, "salt": salt, "role": role}


def verify_password(entry, password):
    """验证 PBKDF2 新条目，兼容旧的无 salt SHA-256 条目。"""
    if not isinstance(entry, dict):
        return False, False
    stored = str(entry.get("password_hash") or "")
    salt = entry.get("salt")
    if salt:
        try:
            candidate, _ = password_hash_pbkdf2(password, str(salt))
        except (TypeError, ValueError):
            return False, False
        return hmac.compare_digest(stored, candidate), False
    return bool(stored) and hmac.compare_digest(stored, password_hash(password)), True


def load_users():
    """加载用户表；首次启动创建默认管理员。"""
    try:
        with open(USERS_PATH, encoding="utf-8") as f:
            users = json.load(f)
        if not isinstance(users, dict):
            raise ValueError("用户文件格式错误")
        return users
    except FileNotFoundError:
        users = {"agentcost": make_user_entry("Ac@2026!dash", "admin")}
        save_users(users)
        return users
    except Exception:
        return {}


def save_users(users):
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


# 内存 token 表: token -> {user, role, exp}
TOKENS = {}
TOKEN_TTL = timedelta(hours=12)
TOKEN_LIMIT = 1000
LOGIN_FAILURES = {}  # (username, IP) -> {count, locked_until}
AUTH_LOCK = threading.Lock()


def login_key(username, client_ip):
    return (str(username), str(client_ip))


def login_locked(username, client_ip):
    with AUTH_LOCK:
        state = LOGIN_FAILURES.get(login_key(username, client_ip))
        return bool(state and state.get("locked_until", 0) > time.time())


def record_login_failure(username, client_ip):
    with AUTH_LOCK:
        key = login_key(username, client_ip)
        state = LOGIN_FAILURES.get(key, {"count": 0, "locked_until": 0})
        state["count"] += 1
        if state["count"] >= 5:
            state["locked_until"] = time.time() + 300
            state["count"] = 0
        LOGIN_FAILURES[key] = state
        return state["locked_until"] > time.time()


def clear_login_failures(username, client_ip):
    with AUTH_LOCK:
        LOGIN_FAILURES.pop(login_key(username, client_ip), None)


def make_token(username, role="user"):
    token = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        # dict 保持插入顺序；满额时逐出最早创建的 token。
        while len(TOKENS) >= TOKEN_LIMIT:
            TOKENS.pop(next(iter(TOKENS)))
        TOKENS[token] = {"user": username, "role": role, "exp": datetime.now() + TOKEN_TTL}
    return token


def check_token(token):
    info = get_token_info(token)
    return info["user"] if info else None


def get_token_info(token):
    if not token:
        return None
    with AUTH_LOCK:
        info = TOKENS.get(token)
        if not info:
            return None
        if datetime.now() > info["exp"]:
            TOKENS.pop(token, None)
            return None
        return {"user": info["user"], "role": info.get("role", "user")}


def get_share_info(token):
    """校验只读分享令牌，并返回创建者身份。"""
    configured = str(_config.get("share_token") or "")
    if not configured or not _config.get("share_enabled") or not token:
        return None
    if not hmac.compare_digest(configured, str(token)):
        return None
    return {"user": _config.get("share_user", ""), "role": _config.get("share_role") or "admin"}


def query_db(sql, params=()):
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ensure_usage_schema():
    """为已有数据库补齐字段并建立常用查询/幂等索引。"""
    if not os.path.exists(DB_PATH):
        return
    conn = get_conn()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)").fetchall()}
        if cols and "source" not in cols:
            conn.execute("ALTER TABLE usage_records ADD COLUMN source TEXT DEFAULT 'parser'")
        if cols and "user" not in cols:
            conn.execute("ALTER TABLE usage_records ADD COLUMN user TEXT")
        if cols and "request_id" not in cols:
            conn.execute("ALTER TABLE usage_records ADD COLUMN request_id TEXT")
        if cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_records_ts ON usage_records(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_records_user ON usage_records(user)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_records_model ON usage_records(model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_records_upstream ON usage_records(upstream)")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_parser_request
                            ON usage_records(request_id)
                            WHERE source = 'parser' AND request_id IS NOT NULL""")
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def ensure_schema():
    """建立独立于用量表的持久化告警历史。"""
    conn = get_conn()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            cooldown_until TEXT NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_alert_history_cooldown
                        ON alert_history(type, level, cooldown_until)""")
        conn.commit()
    finally:
        conn.close()


def parse_range(params):
    """解析 from/to 参数，返回 (from_str, to_str)。默认近 7 天。"""
    today = datetime.now().strftime("%Y-%m-%d")
    frm = params.get("from", [""])[0] or (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    to = params.get("to", [""])[0] or today
    return frm, to


def summary(frm, to, agent=None, upstream=None, model=None, group_by="day", user=None, cwd=None):
    """范围汇总。"""
    if group_by not in ("day", "hour"):
        group_by = "day"
    def agg(cond, params):
        r = query_db(f"""
            SELECT COUNT(*) as cnt,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(cost_usd),0) as cost,
                   COALESCE(SUM(input_tokens),0) as inp_tokens,
                   COALESCE(SUM(cache_read_tokens),0) as cr_tokens,
                   COALESCE(SUM(cache_write_tokens),0) as cw_tokens
            FROM usage_records WHERE {cond}
        """, params)[0]
        # 平均缓存率 = 缓存读 / (新输入 + 缓存读 + 缓存写)
        total_in = r["inp_tokens"] + r["cr_tokens"] + r["cw_tokens"]
        r["cache_rate"] = round(r["cr_tokens"] / total_in * 100, 1) if total_in else 0
        return r

    # 所有汇总都复用同一组可选筛选条件，保证范围、预测和环比口径一致。
    filter_cond = []
    filter_params = []
    if agent:
        filter_cond.append("agent = ?")
        filter_params.append(agent)
    if upstream:
        filter_cond.append("upstream = ?")
        filter_params.append(upstream)
    if model:
        filter_cond.append("model = ?")
        filter_params.append(model)
    if user:
        filter_cond.append("user = ?")
        filter_params.append(user)
    if cwd:
        filter_cond.append("cwd = ?")
        filter_params.append(cwd)

    def filtered(cond, params):
        parts = [cond, *filter_cond]
        return " AND ".join(parts), tuple(params) + tuple(filter_params)

    # 范围 = ts >= frm 且 ts <= to+1天
    to_next = (datetime.strptime(to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    base_cond = "ts >= ? AND ts < ?"
    base_params = (frm, to_next)
    range_cond, range_params = filtered(base_cond, base_params)

    fx = fetch_fx_rate()

    def with_cny(obj, keys=("cost",)):
        """给金额对象附加人民币字段。"""
        for k in keys:
            v = obj.get(k) or 0
            # saved_usd 的人民币字段按 API 约定命名为 saved_cny。
            out_key = "saved_cny" if k == "saved_usd" else k + "_cny"
            obj[out_key] = round(v * fx, 2)
        return obj

    def with_cache(m):
        """给模型行附加缓存率。缓存率 = 缓存读 / (新输入 + 缓存读 + 缓存写)。"""
        inp = m.get("inp_tokens") or 0
        cr = m.get("cr_tokens") or 0
        cw = m.get("cw_tokens") or 0
        total_in = inp + cr + cw
        m["cache_rate"] = round(cr / total_in * 100, 1) if total_in else 0
        m["cache_inp_tokens"] = inp
        m["cache_cr_tokens"] = cr
        m["cache_cw_tokens"] = cw
        # 缓存价格：优先用户覆盖，否则 = 输入价 × 系数（读 0.1，写 1.25）
        ovr = load_models_override()
        ovr_entry = ovr.get(m["model"]) or ovr.get(m["model"].split("/")[-1]) or {}
        if ovr_entry.get("cache_price_in") is not None:
            m["cache_price_in"] = float(ovr_entry["cache_price_in"])
        elif m.get("price_in"):
            m["cache_price_in"] = round(m["price_in"] * 0.1, 4)
        else:
            m["cache_price_in"] = None
        if ovr_entry.get("cache_price_write") is not None:
            m["cache_price_write"] = float(ovr_entry["cache_price_write"])
        elif m.get("price_in"):
            m["cache_price_write"] = round(m["price_in"] * 1.25, 4)
        else:
            m["cache_price_write"] = None
        return m

    total = agg(range_cond, range_params)
    total["saved_usd"] = 0

    # 月底预测：本月已用成本 / 已过天数 × 本月总天数（金额均为 USD）。
    now = datetime.now()
    month_start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month_dt = (month_start_dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_start = month_start_dt.strftime("%Y-%m-%d")
    # 截止今天（含今天），避免未来日期记录影响“已用”。
    today_exclusive = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    month_cond, month_params = filtered("ts >= ? AND ts < ?", (month_start, today_exclusive))
    month_used = agg(month_cond, month_params)["cost"] or 0
    days_elapsed = now.day
    days_total = (next_month_dt - month_start_dt).days
    month_avg = month_used / days_elapsed if days_elapsed else 0
    forecast_monthly = {
        "used": round(month_used, 8),
        "avg": round(month_avg, 8),
        "forecast": round(month_avg * days_total, 8),
        "days_elapsed": days_elapsed,
        "days_total": days_total,
    }

    # 环比：将所选范围整体向前平移同样的天数。
    frm_dt = datetime.strptime(frm, "%Y-%m-%d")
    to_dt = datetime.strptime(to, "%Y-%m-%d")
    range_days = (to_dt - frm_dt).days + 1
    prev_from_dt = frm_dt - timedelta(days=range_days)
    prev_to_dt = to_dt - timedelta(days=range_days)
    prev_from = prev_from_dt.strftime("%Y-%m-%d")
    prev_to = prev_to_dt.strftime("%Y-%m-%d")
    prev_cond, prev_params = filtered(
        "ts >= ? AND ts < ?",
        (prev_from, (prev_to_dt + timedelta(days=1)).strftime("%Y-%m-%d")),
    )
    prev = agg(prev_cond, prev_params)
    prev.update({"from": prev_from, "to": prev_to})
    by_agent = query_db(f"""
        SELECT agent, COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost,
               SUM(input_tokens) as inp_tokens, SUM(output_tokens) as out_tokens,
               SUM(cache_read_tokens) as cr_tokens, SUM(cache_write_tokens) as cw_tokens,
               SUM(reasoning_tokens) as reasoning_tokens
        FROM usage_records WHERE {range_cond} GROUP BY agent
    """, range_params)
    by_user = query_db(f"""
        SELECT user, COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost,
               SUM(input_tokens) as inp_tokens, SUM(output_tokens) as out_tokens,
               SUM(cache_read_tokens) as cr_tokens, SUM(cache_write_tokens) as cw_tokens,
               SUM(reasoning_tokens) as reasoning_tokens
        FROM usage_records WHERE {range_cond} AND user IS NOT NULL AND user != ''
        GROUP BY user ORDER BY tokens DESC
    """, range_params)
    by_model = query_db(f"""
        SELECT model, upstream, billing_mode, price_in, price_out,
               COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost,
               SUM(input_tokens) as inp_tokens, SUM(output_tokens) as out_tokens,
               SUM(cache_read_tokens) as cr_tokens, SUM(cache_write_tokens) as cw_tokens,
               SUM(reasoning_tokens) as reasoning_tokens
        FROM usage_records WHERE {range_cond} GROUP BY model, upstream ORDER BY tokens DESC
    """, range_params)
    by_project = query_db(f"""
        SELECT cwd, COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost,
               SUM(input_tokens) as inp_tokens, SUM(output_tokens) as out_tokens,
               SUM(cache_read_tokens) as cr_tokens, SUM(cache_write_tokens) as cw_tokens,
               SUM(reasoning_tokens) as reasoning_tokens
        FROM usage_records WHERE {range_cond} AND cwd IS NOT NULL AND cwd != ''
        GROUP BY cwd ORDER BY cost DESC
    """, range_params)
    day_expr = "substr(ts,1,13)" if group_by == "hour" else "substr(ts,1,10)"
    daily = query_db(f"""
        SELECT {day_expr} as day, COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_write_tokens) as cw,
               SUM(reasoning_tokens) as reasoning
        FROM usage_records WHERE {range_cond} GROUP BY day ORDER BY day
    """, range_params)

    # 按日/小时、模型、上游计算缓存节省，再汇总到 daily 对应粒度。
    daily_cache = query_db(f"""
        SELECT {day_expr} as day, model, upstream,
               SUM(cache_read_tokens) as cr, MIN(price_in) as pi
        FROM usage_records WHERE {range_cond}
        GROUP BY day, model, upstream
    """, range_params)
    daily_saved = {}
    model_overrides = load_models_override()
    for row in daily_cache:
        cr = row.get("cr") or 0
        pi = row.get("pi")
        override = model_overrides.get(row.get("model")) or model_overrides.get((row.get("model") or "").split("/")[-1]) or {}
        cache_pi = override.get("cache_price_in") if override.get("cache_price_in") is not None else (float(pi) * 0.1 if pi is not None else None)
        saved = cr / 1e6 * (float(pi) - float(cache_pi)) if cr and pi is not None and cache_pi is not None else 0
        daily_saved[row["day"]] = daily_saved.get(row["day"], 0) + saved
    for row in daily:
        row["saved_usd"] = round(daily_saved.get(row["day"], 0), 8)

    # 缓存节省：若无缓存，缓存读 token 按输入价计费；实际按缓存读价计费。
    for m in by_model:
        with_cache(m)
        cr = m.get("cr_tokens") or 0
        price_in = m.get("price_in")
        cache_price_in = m.get("cache_price_in")
        if cr and price_in is not None and cache_price_in is not None:
            m["saved_usd"] = cr / 1e6 * (float(price_in) - float(cache_price_in))
        else:
            m["saved_usd"] = 0
        total["saved_usd"] += m["saved_usd"]
    total["saved_usd"] = round(total["saved_usd"], 8)

    return {
        "last_scan": LAST_SCAN_AT,
        "min_ts": ((query_db("SELECT MIN(ts) AS min_ts FROM usage_records" + (" WHERE user = ?" if user else ""), (user,) if user else ())[0].get("min_ts") or "")[:10] or None),
        "range": {"from": frm, "to": to},
        "fx": fx,
        "total": with_cny(total, ("cost", "saved_usd")),
        "by_agent": [with_cny(a) for a in by_agent],
        "by_user": [with_cny(u) for u in by_user],
        "by_model": [with_cny(m, ("cost", "saved_usd")) for m in by_model],
        "by_project": [with_cny(p) for p in by_project],
        "daily": [with_cny(x) for x in daily],
        "forecast_monthly": forecast_monthly,
        "prev": prev,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        # 所有静态响应都必须留在 STATIC_DIR 内，禁止 /static/../../... 路径穿越。
        static_root = os.path.realpath(STATIC_DIR)
        real_path = os.path.realpath(path)
        try:
            inside_static = os.path.commonpath((static_root, real_path)) == static_root
        except ValueError:
            inside_static = False
        if not inside_static or not os.path.isfile(real_path):
            self.send_error(404)
            return
        with open(real_path, "rb") as f:
            body = f.read()
        ctype = "text/html; charset=utf-8"
        if real_path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif real_path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_backup_file(self, name):
        """下载备份文件；文件名必须解析到当前备份目录内。"""
        root = backup_dir()
        candidate = os.path.realpath(os.path.join(root, name))
        try:
            inside_backup = os.path.commonpath((root, candidate)) == root
        except ValueError:
            inside_backup = False
        if not inside_backup or os.path.basename(candidate) != name or not os.path.isfile(candidate):
            self.send_json({"ok": False, "error": "备份文件不存在"}, status=404)
            return
        with open(candidate, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def check_ingest_auth(self):
        """接入 API 支持登录 token，或配置的独立 X-Ingest-Key。"""
        info = get_token_info(self.headers.get("X-Token", ""))
        if info:
            return "token", info
        ingest_key = str(_config.get("ingest_key") or "")
        supplied = self.headers.get("X-Ingest-Key", "")
        if ingest_key and supplied and hmac.compare_digest(ingest_key, supplied):
            return "ingest_key", None
        return None, None

    def ingest(self, data, auth_kind, token_info):
        """校验并批量写入 API 用量记录。"""
        items = data if isinstance(data, list) else [data]
        if not items or any(not isinstance(item, dict) for item in items):
            self.send_json({"ok": False, "error": "请求体必须是对象或对象数组"}, status=400)
            return

        int_fields = ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "reasoning_tokens", "total_tokens")
        str_fields = ("agent", "model", "provider", "upstream", "base_url",
                      "billing_mode", "cwd", "originator")
        rows = []
        for item in items:
            model = item.get("model")
            if model is None or not str(model).strip():
                self.send_json({"ok": False, "error": "缺少模型名"}, status=400)
                return
            model = str(model).strip()
            ts_value = item.get("ts")
            if ts_value is None or ts_value == "":
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                try:
                    ts_text = str(ts_value).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ts_text)
                    if dt.tzinfo:
                        dt = dt.astimezone()
                    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "ts 必须是 ISO 时间字符串"}, status=400)
                    return
            values = {}
            try:
                for field in int_fields:
                    value = item.get(field, 0)
                    values[field] = 0 if value is None or value == "" else int(value)
                cost = item.get("cost_usd", 0)
                values["cost_usd"] = 0.0 if cost is None or cost == "" else float(cost)
            except (TypeError, ValueError, OverflowError):
                self.send_json({"ok": False, "error": "数字字段格式错误"}, status=400)
                return
            request_id = item.get("request_id")
            if request_id is not None and not isinstance(request_id, str):
                self.send_json({"ok": False, "error": "request_id 必须是字符串"}, status=400)
                return
            requested_user = item.get("user")
            if requested_user is not None and (not isinstance(requested_user, str) or not requested_user.strip()):
                self.send_json({"ok": False, "error": "user 必须是非空字符串"}, status=400)
                return
            record_user = requested_user.strip() if isinstance(requested_user, str) else None
            if auth_kind == "ingest_key" and not record_user:
                self.send_json({"ok": False, "error": "X-Ingest-Key 认证时 user 必填"}, status=400)
                return
            if auth_kind == "token" and not record_user:
                record_user = token_info["user"]
            rows.append((
                item.get("agent", ""), ts, model,
                *(str(item.get(field, "") or "") for field in str_fields[2:]),
                values["input_tokens"], values["output_tokens"],
                values["cache_read_tokens"], values["cache_write_tokens"],
                values["reasoning_tokens"], values["total_tokens"], values["cost_usd"], "api",
                request_id, record_user,
            ))

        conn = get_conn()
        try:
            cur = conn.cursor()
            cols = {row[1] for row in cur.execute("PRAGMA table_info(usage_records)").fetchall()}
            if "source" not in cols:
                cur.execute("ALTER TABLE usage_records ADD COLUMN source TEXT DEFAULT 'parser'")
            if "request_id" not in cols:
                cur.execute("ALTER TABLE usage_records ADD COLUMN request_id TEXT")
            if "user" not in cols:
                cur.execute("ALTER TABLE usage_records ADD COLUMN user TEXT")
            insert_rows = []
            skipped = 0
            seen_request_ids = set()
            for row in rows:
                # rows 结构末尾: (..., request_id, record_user)，request_id 在倒数第二位
                request_id = row[-2]
                if request_id is not None:
                    exists = cur.execute(
                        "SELECT COUNT(*) FROM usage_records WHERE source='api' AND request_id = ?",
                        (request_id,),
                    ).fetchone()[0]
                    if exists or request_id in seen_request_ids:
                        skipped += 1
                        continue
                    seen_request_ids.add(request_id)
                insert_rows.append(row)
            cur.executemany("""
                INSERT INTO usage_records
                (agent, ts, model, provider, upstream, base_url, billing_mode, price_in, price_out,
                 cwd, originator, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                 reasoning_tokens, total_tokens, cost_usd, source, request_id, user)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [row[:7] + (None, None) + row[7:] for row in insert_rows])
            conn.commit()
        finally:
            conn.close()
        self.send_json({"ok": True, "inserted": len(insert_rows), "skipped": skipped, "duplicates": skipped})

    def do_POST(self):
        if self.path == "/api/login":
            data = self.read_body()
            username = data.get("username", "")
            password = data.get("password", "")
            client_ip = self.client_address[0]
            if login_locked(username, client_ip):
                self.send_json({"ok": False, "error": "登录失败次数过多，请 5 分钟后重试"}, status=429)
                return
            users = load_users()
            entry = users.get(username) if isinstance(username, str) else None
            role = entry.get("role", "user") if isinstance(entry, dict) else "user"
            valid, legacy = verify_password(entry, password)
            if valid:
                # 成功使用旧 SHA-256 登录后就地升级，users.json 之后仅写 PBKDF2 条目。
                if legacy:
                    users[username] = make_user_entry(password, role)
                    save_users(users)
                clear_login_failures(username, client_ip)
                token = make_token(username, role)
                self.send_json({"ok": True, "token": token, "user": username, "role": role})
            else:
                locked = record_login_failure(username, client_ip)
                if locked:
                    self.send_json({"ok": False, "error": "登录失败次数过多，请 5 分钟后重试"}, status=429)
                else:
                    self.send_json({"ok": False, "error": "用户名或密码错误"}, status=401)
        elif self.path == "/api/ingest":
            auth_kind, token_info = self.check_ingest_auth()
            if not auth_kind:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            self.ingest(self.read_body(), auth_kind, token_info)
        elif self.path == "/api/users":
            info = get_token_info(self.headers.get("X-Token", ""))
            if not info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            if info["role"] != "admin":
                self.send_json({"ok": False, "error": "无权限"}, status=403)
                return
            data = self.read_body()
            username = data.get("username")
            password = data.get("password", "")
            role = data.get("role", "user")
            if not isinstance(username, str) or not username.strip() or role not in ("admin", "user"):
                self.send_json({"ok": False, "error": "username 和 role（admin/user）必填"}, status=400)
                return
            if not isinstance(password, str):
                self.send_json({"ok": False, "error": "password 必须是字符串"}, status=400)
                return
            username = username.strip()
            users = load_users()
            old = users.get(username, {})
            entry = dict(old) if isinstance(old, dict) else {}
            entry["role"] = role
            if password:
                entry = make_user_entry(password, role)
            if not entry["password_hash"]:
                self.send_json({"ok": False, "error": "新用户必须提供 password"}, status=400)
                return
            users[username] = entry
            save_users(users)
            self.send_json({"ok": True, "user": {"username": username, "role": role}})
        elif self.path == "/api/config":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            data = self.read_body()
            try:
                cfg = save_config(data)
                self.send_json({"ok": True, "config": cfg})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=400)
        elif self.path == "/api/backups":
            info = get_token_info(self.headers.get("X-Token", ""))
            if not info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            if info["role"] != "admin":
                self.send_json({"ok": False, "error": "无权限"}, status=403)
                return
            try:
                name = create_backup()
                self.send_json({"ok": True, "file": name})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
        elif self.path == "/api/test-webhook":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            ok = send_webhook("✅ AgentCost 测试通知：Webhook 配置成功！")
            self.send_json({"ok": ok, "sent": ok, "error": None if ok else "发送失败，请检查 URL 和类型"})
        elif self.path == "/api/prices/fetch":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            fetched, error = cost_parser.fetch_official_prices()
            if fetched:
                global _last_price_fetch_date
                _last_price_fetch_date = datetime.now().astimezone().date().isoformat()
                run_parser()
                cached = cost_parser.load_official_prices()
                self.send_json({"ok": True, "fetched": fetched, "fetched_at": cached.get("fetched_at", "")})
            else:
                self.send_json({"ok": False, "fetched": 0, "error": error or "抓取失败"}, status=502)
        elif self.path == "/api/models":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            data = self.read_body()
            # data: {"model": "xxx", "upstream": "...", "price_in": 1.2, "price_out": 3.4, "billing_mode": "api"}
            model = (data.get("model") or "").strip()
            if not model:
                self.send_json({"ok": False, "error": "缺少模型名"}, status=400)
                return
            ovr = load_models_override()
            entry = {}
            if data.get("upstream"):
                entry["upstream"] = str(data["upstream"]).strip()
            pi, po = data.get("price_in"), data.get("price_out")
            if pi is not None and po is not None:
                try:
                    entry["price_in"] = float(pi)
                    entry["price_out"] = float(po)
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "价格必须是数字"}, status=400)
                    return
            if data.get("billing_mode"):
                entry["billing_mode"] = str(data["billing_mode"]).strip()
            cpi, cpw = data.get("cache_price_in"), data.get("cache_price_write")
            if cpi is not None or cpw is not None:
                try:
                    if cpi is not None:
                        entry["cache_price_in"] = float(cpi)
                    if cpw is not None:
                        entry["cache_price_write"] = float(cpw)
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "缓存价格必须是数字"}, status=400)
                    return
            if not entry:
                # 空覆盖 = 删除该模型的覆盖
                ovr.pop(model, None)
            else:
                ovr[model] = entry
            save_models_override(ovr)
            # 立即刷新数据让新价格生效
            run_parser()
            self.send_json({"ok": True, "overrides": ovr})
        else:
            self.send_error(404)

    def send_export_csv(self, frm, to, user=None):
        """导出所选范围的用量明细为 CSV。"""
        to_next = (datetime.strptime(to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        conn = get_conn()
        where = "ts >= ? AND ts < ?" + (" AND user = ?" if user else "")
        rows = conn.execute(f"""
            SELECT ts, agent, model, upstream, billing_mode,
                   input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   total_tokens, cost_usd
            FROM usage_records WHERE {where}
            ORDER BY ts
        """, (frm, to_next, user) if user else (frm, to_next)).fetchall()
        conn.close()

        buf = io.StringIO()
        buf.write("\ufeff")  # UTF-8 BOM，Excel 打开中文不乱码
        writer = csv.writer(buf)
        writer.writerow(["时间", "Agent", "模型", "上游", "计费模式",
                         "输入Token", "输出Token", "缓存读Token", "缓存写Token",
                         "总Token", "成本USD", "成本CNY"])
        fx = fetch_fx_rate()
        for r in rows:
            ts, agent, model, upstream, bm, inp, out, cr, cw, total, cost = r
            writer.writerow([ts, agent, model, upstream, bm,
                             inp or 0, out or 0, cr or 0, cw or 0,
                             total or 0, round(cost or 0, 4), round((cost or 0) * fx, 2)])
        data = buf.getvalue().encode("utf-8")
        fname = f"agentcost_{frm}_to_{to}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        params = parse_qs(urlparse(self.path).query)
        token = self.headers.get("X-Token", "")
        token_info = get_token_info(token)
        share_info = (get_share_info(params.get("token", [""])[0]) or
                      get_share_info(self.headers.get("X-Share-Token", "")))

        def scoped_user(info, requested=None):
            """普通用户及其分享链接始终仅能查询自己的归属数据。"""
            if info and info.get("role") != "admin":
                return info["user"]
            requested = (requested or "").strip()
            return None if requested in ("", "all") else requested

        if path == "/api/health":
            records = 0
            db_ok = False
            try:
                conn = get_conn()
                try:
                    records = conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
                    db_ok = True
                finally:
                    conn.close()
            except sqlite3.Error:
                # 数据库尚未初始化或不可用时，仍返回服务进程的健康状态。
                pass
            self.send_json({
                "ok": True,
                "version": __version__,
                "db_ok": db_ok,
                "records": records,
                "uptime_seconds": int(time.time() - START_TIME),
                "last_scan": LAST_SCAN_AT,
            })
        elif path == "/api/summary":
            identity = token_info or share_info
            if not identity:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            frm, to = parse_range(params)
            agent = params.get("agent", [""])[0].strip()
            upstream = params.get("upstream", [""])[0].strip()
            model = params.get("model", [""])[0].strip()
            cwd = params.get("cwd", [""])[0].strip()
            group_by = params.get("group_by", ["day"])[0].strip().lower()
            user = scoped_user(identity, params.get("user", [""])[0])
            self.send_json({"ok": True, **summary(
                frm, to, agent=agent or None, upstream=upstream or None,
                model=model or None, group_by=group_by, user=user, cwd=cwd or None,
            )})
        elif path == "/api/records":
            identity = token_info or share_info
            if not identity:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            frm, to = parse_range(params)
            try:
                limit = max(1, min(int(params.get("limit", ["100"])[0]), 500))
                offset = max(0, int(params.get("offset", ["0"])[0]))
            except ValueError:
                self.send_json({"ok": False, "error": "limit 和 offset 必须为整数"}, status=400)
                return
            filters, values = ["ts >= ?", "ts < ?"], [frm, (datetime.strptime(to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")]
            for column in ("agent", "upstream", "model", "cwd"):
                value = params.get(column, [""])[0].strip()
                if value:
                    filters.append(f"{column} = ?")
                    values.append(value)
            user = scoped_user(identity, params.get("user", [""])[0])
            if user:
                filters.append("user = ?")
                values.append(user)
            where = " AND ".join(filters)
            total = query_db(f"SELECT COUNT(*) AS total FROM usage_records WHERE {where}", tuple(values))[0]["total"]
            rows = query_db(f"""
                SELECT id, ts, agent, model, upstream, cwd, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens,
                       cost_usd, user
                FROM usage_records WHERE {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?
            """, tuple(values) + (limit, offset))
            fx = fetch_fx_rate()
            for row in rows:
                row["cost_cny"] = round((row.get("cost_usd") or 0) * fx, 2)
            self.send_json({"ok": True, "total": total, "records": rows})
        elif path == "/api/filters":
            identity = token_info or share_info
            if not identity:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            user = scoped_user(identity, params.get("user", [""])[0])
            where = "WHERE ((agent IS NOT NULL AND agent != '') OR (upstream IS NOT NULL AND upstream != ''))"
            query_params = ()
            if user:
                where += " AND user = ?"
                query_params = (user,)
            rows = query_db(f"""
                SELECT DISTINCT agent, upstream FROM usage_records
                {where}
            """, query_params)
            agents = sorted({row["agent"] for row in rows if row["agent"]})
            upstreams = sorted({row["upstream"] for row in rows if row["upstream"]})
            project_where = "WHERE cwd IS NOT NULL AND cwd != ''"
            project_params = ()
            if user:
                project_where += " AND user = ?"
                project_params = (user,)
            projects = [r["cwd"] for r in query_db(
                f"SELECT DISTINCT cwd FROM usage_records {project_where} ORDER BY cwd", project_params
            )]
            users = []
            if identity.get("role") == "admin":
                users = [r["user"] for r in query_db(
                    "SELECT DISTINCT user FROM usage_records WHERE user IS NOT NULL AND user != '' ORDER BY user"
                )]
            elif identity.get("user"):
                users = [identity["user"]]
            self.send_json({"ok": True, "agents": agents, "upstreams": upstreams, "projects": projects, "users": users})
        elif path == "/api/config":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            # 返回配置（脱敏：webhook_url 只回显前 30 字符）
            cfg = dict(_config)
            if cfg.get("webhook_url"):
                cfg["webhook_url_masked"] = cfg["webhook_url"][:30] + "..." if len(cfg["webhook_url"]) > 30 else cfg["webhook_url"]
            cfg["fx"] = fetch_fx_rate()
            self.send_json({"ok": True, "config": cfg})
        elif path == "/api/backups":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            if token_info["role"] != "admin":
                self.send_json({"ok": False, "error": "无权限"}, status=403)
                return
            self.send_json({"ok": True, "backup_dir": backup_dir(), "files": list_backups()})
        elif path == "/api/backups/download":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            if token_info["role"] != "admin":
                self.send_json({"ok": False, "error": "无权限"}, status=403)
                return
            self.send_backup_file(params.get("name", [""])[0])
        elif path == "/api/alerts":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            self.send_json({"ok": True, "alerts": query_db(
                "SELECT ts, level, type, message FROM alert_history ORDER BY id DESC LIMIT 50"
            )})
        elif path == "/api/models":
            identity = token_info or share_info
            if not identity:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            self.send_json({"ok": True, **model_catalog(scoped_user(identity, params.get("user", [""])[0]))})
        elif path == "/api/prices":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            data = official_prices_for_local_models()
            self.send_json({"ok": True, "fetched_at": data["fetched_at"],
                            "count": len(data["prices"]), "prices": data["prices"]})
        elif path == "/api/export":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            frm, to = parse_range(params)
            self.send_export_csv(frm, to, scoped_user(token_info, params.get("user", [""])[0]))
        elif path == "/api/users":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            if token_info["role"] != "admin":
                self.send_json({"ok": False, "error": "无权限"}, status=403)
                return
            users = load_users()
            self.send_json({"ok": True, "users": [
                {"username": username, "role": entry.get("role", "user")}
                for username, entry in sorted(users.items()) if isinstance(entry, dict)
            ]})
        elif path == "/api/share/create":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            share_token = secrets.token_urlsafe(16)
            save_config({"share_enabled": True, "share_token": share_token,
                         "share_user": token_info["user"], "share_role": token_info["role"]})
            self.send_json({"ok": True, "share_url": "/share/?token=" + share_token})
        elif path == "/api/share/revoke":
            if not token_info:
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            save_config({"share_enabled": False, "share_token": "", "share_user": "", "share_role": ""})
            self.send_json({"ok": True})
        elif path == "/share/" or path == "/share":
            # 重定向到根路径（保留 ?token= 参数），保证页面内相对路径 api/... 正确解析
            q = urlparse(self.path).query
            self.send_response(302)
            self.send_header("Location", "/" + ("?" + q if q else ""))
            self.end_headers()
        elif path == "/" or path == "/index.html":
            self.send_file(os.path.join(STATIC_DIR, "index.html"))
        elif path.startswith("/static/"):
            self.send_file(os.path.join(STATIC_DIR, path[len("/static/"):]))
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        params = parse_qs(urlparse(self.path).query)
        if path != "/api/users":
            self.send_error(404)
            return
        info = get_token_info(self.headers.get("X-Token", ""))
        if not info:
            self.send_json({"ok": False, "error": "未登录"}, status=401)
            return
        if info["role"] != "admin":
            self.send_json({"ok": False, "error": "无权限"}, status=403)
            return
        username = params.get("username", [""])[0].strip()
        users = load_users()
        if not username or username not in users:
            self.send_json({"ok": False, "error": "用户不存在"}, status=404)
            return
        if username == info["user"]:
            self.send_json({"ok": False, "error": "不允许删除自己"}, status=400)
            return
        if users[username].get("role") == "admin":
            admins = sum(1 for entry in users.values() if isinstance(entry, dict) and entry.get("role") == "admin")
            if admins <= 1:
                self.send_json({"ok": False, "error": "不允许删除最后一个管理员"}, status=400)
                return
        del users[username]
        save_users(users)
        self.send_json({"ok": True})


def main():
    print(f"AgentCost v3 仪表盘: http://{HOST}:{PORT}")
    print(f"自动刷新: 每 {AUTO_REFRESH_SECONDS} 秒")
    # 自定义数据目录（例如 Docker 的 /data）可能尚不存在。
    os.makedirs(DB_DIR, exist_ok=True)
    # 在接受请求前加载配置，确保独立接入 Key 立即生效。
    load_config()
    load_users()
    ensure_usage_schema()
    ensure_schema()
    # 启动自动刷新线程（守护线程，随主进程退出）
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
