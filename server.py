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
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_DIR = os.environ.get("AGENTCOST_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "agentcost.db")
PARSER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parser.py")
OVERRIDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_override.json")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PORT = int(os.environ.get("AGENTCOST_PORT", "8666"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
AUTO_REFRESH_SECONDS = int(os.environ.get("AGENTCOST_REFRESH_SECONDS", "300"))  # 默认每 5 分钟自动刷新

# ---- 自动刷新：定时重跑 parser 更新 DB ----
def run_parser():
    try:
        result = subprocess.run(
            [sys.executable, PARSER_PATH],
            capture_output=True, text=True, timeout=180
        )
        return result.returncode == 0
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
}
_config = dict(DEFAULT_CONFIG)
_last_notify = {}  # level -> timestamp


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


def model_catalog():
    """返回所有已知模型 + 当前上游/价格 + 是否被用户覆盖。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT model, upstream, price_in, price_out, billing_mode,
               COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost
        FROM usage_records GROUP BY model, upstream ORDER BY tokens DESC
    """).fetchall()
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
        conn = sqlite3.connect(DB_PATH)
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

        level = None
        if cost_usd >= budget_usd:
            level = "over"
        elif pct >= threshold:
            level = "warn"

        if level is None:
            return
        # 冷却期检查
        last = _last_notify.get(level, 0)
        cooldown = _config.get("notify_cooldown_hours", 12) * 3600
        if now - last < cooldown:
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
    except Exception as e:
        print(f"[budget] 检查失败: {e}")


def auto_refresh_loop():
    """后台线程：启动时刷一次，之后每 5 分钟刷一次；每次刷新后检查预算。"""
    run_parser()
    load_config()
    check_budget_and_notify()
    while True:
        time.sleep(AUTO_REFRESH_SECONDS)
        try:
            run_parser()
            check_budget_and_notify()
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

# ---- 认证配置 ----
# 用户名: 密码（明文仅用于个人部署；生产请换 hash）
AUTH_USERS = {"agentcost": "Ac@2026!dash"}
# 内存 token 表: token -> username
TOKENS = {}
TOKEN_TTL = timedelta(hours=12)


def make_token(username):
    token = secrets.token_urlsafe(32)
    TOKENS[token] = {"user": username, "exp": datetime.now() + TOKEN_TTL}
    return token


def check_token(token):
    if not token:
        return None
    info = TOKENS.get(token)
    if not info:
        return None
    if datetime.now() > info["exp"]:
        TOKENS.pop(token, None)
        return None
    return info["user"]


def query_db(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def parse_range(params):
    """解析 from/to 参数，返回 (from_str, to_str)。默认本月。"""
    today = datetime.now().strftime("%Y-%m-%d")
    frm = params.get("from", [""])[0] or datetime.now().strftime("%Y-%m-01")
    to = params.get("to", [""])[0] or today
    return frm, to


def summary(frm, to):
    """范围汇总。"""
    def agg(cond, params):
        r = query_db(f"""
            SELECT COUNT(*) as cnt,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(cost_usd),0) as cost
            FROM usage_records WHERE {cond}
        """, params)[0]
        return r

    # 范围 = ts >= frm 且 ts <= to+1天
    to_next = (datetime.strptime(to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    base_cond = "ts >= ? AND ts < ?"
    base_params = (frm, to_next)

    fx = fetch_fx_rate()

    def with_cny(obj, keys=("cost",)):
        """给金额对象附加人民币字段。"""
        for k in keys:
            v = obj.get(k) or 0
            obj[k + "_cny"] = round(v * fx, 2)
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

    total = agg(base_cond, base_params)
    by_agent = query_db(f"""
        SELECT agent, COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost
        FROM usage_records WHERE {base_cond} GROUP BY agent
    """, base_params)
    by_model = query_db(f"""
        SELECT model, upstream, billing_mode, price_in, price_out,
               COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost,
               SUM(input_tokens) as inp_tokens, SUM(output_tokens) as out_tokens,
               SUM(cache_read_tokens) as cr_tokens, SUM(cache_write_tokens) as cw_tokens
        FROM usage_records WHERE {base_cond} GROUP BY model, upstream ORDER BY tokens DESC LIMIT 20
    """, base_params)
    daily = query_db(f"""
        SELECT substr(ts,1,10) as day, COUNT(*) as cnt, SUM(total_tokens) as tokens, SUM(cost_usd) as cost
        FROM usage_records WHERE {base_cond} GROUP BY day ORDER BY day
    """, base_params)

    return {
        "range": {"from": frm, "to": to},
        "fx": fx,
        "total": with_cny(total),
        "by_agent": [with_cny(a) for a in by_agent],
        "by_model": [with_cny(with_cache(m)) for m in by_model],
        "daily": [with_cny(x) for x in daily],
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
        if not os.path.exists(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        ctype = "text/html; charset=utf-8"
        if path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
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

    def do_POST(self):
        if self.path == "/api/login":
            data = self.read_body()
            username = data.get("username", "")
            password = data.get("password", "")
            stored = AUTH_USERS.get(username)
            if stored and hmac.compare_digest(stored, password):
                token = make_token(username)
                self.send_json({"ok": True, "token": token, "user": username})
            else:
                self.send_json({"ok": False, "error": "用户名或密码错误"}, status=401)
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
        elif self.path == "/api/test-webhook":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            ok = send_webhook("✅ AgentCost 测试通知：Webhook 配置成功！")
            self.send_json({"ok": ok, "sent": ok, "error": None if ok else "发送失败，请检查 URL 和类型"})
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

    def send_export_csv(self, frm, to):
        """导出所选范围的用量明细为 CSV。"""
        to_next = (datetime.strptime(to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT ts, agent, model, upstream, billing_mode,
                   input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   total_tokens, cost_usd
            FROM usage_records WHERE ts >= ? AND ts < ?
            ORDER BY ts
        """, (frm, to_next)).fetchall()
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

        if path == "/api/summary":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            frm, to = parse_range(params)
            self.send_json({"ok": True, **summary(frm, to)})
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
        elif path == "/api/models":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            self.send_json({"ok": True, **model_catalog()})
        elif path == "/api/export":
            token = self.headers.get("X-Token", "")
            if not check_token(token):
                self.send_json({"ok": False, "error": "未登录"}, status=401)
                return
            frm, to = parse_range(params)
            self.send_export_csv(frm, to)
        elif path == "/" or path == "/index.html":
            self.send_file(os.path.join(STATIC_DIR, "index.html"))
        elif path.startswith("/static/"):
            self.send_file(os.path.join(STATIC_DIR, path[len("/static/"):]))
        else:
            self.send_error(404)


def main():
    print(f"AgentCost v3 仪表盘: http://localhost:{PORT}")
    print(f"自动刷新: 每 {AUTO_REFRESH_SECONDS} 秒")
    # 启动自动刷新线程（守护线程，随主进程退出）
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
