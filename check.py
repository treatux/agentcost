#!/usr/bin/env python3
"""
AgentCost - 预算告警 + 自动刷新
1. 刷新数据：调 parser 重扫日志
2. 检查预算：超阈值发送微信提醒（通过 Hermes cron 通道）
用法：
  python3 check.py              # 刷新+检查，超预算打印告警
  python3 check.py --alert      # 刷新+检查+发送微信
"""
import os
import sys
import json
import sqlite3
import subprocess
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "agentcost.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "monthly_budget_usd": 50.0,
    "alert_threshold": 0.8,       # 80% 告警
    "wechat_alert": True
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    return dict(DEFAULT_CONFIG)


def refresh():
    """重跑 parser 刷新数据库。"""
    parser = os.path.join(BASE_DIR, "parser.py")
    result = subprocess.run([sys.executable, parser], capture_output=True, text=True, timeout=120)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
    return result.returncode == 0


def month_cost():
    """本月累计成本。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    month_start = datetime.now().strftime("%Y-%m-01")
    row = cur.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM usage_records WHERE ts >= ?",
        (month_start,)
    ).fetchone()
    conn.close()
    return row[0], row[1]


def main():
    cfg = load_config()
    refresh_ok = refresh()
    if not refresh_ok:
        print("[!] 数据刷新失败")
        sys.exit(1)

    cost, cnt = month_cost()
    budget = cfg["monthly_budget_usd"]
    pct = cost / budget * 100 if budget else 0
    threshold = cfg["alert_threshold"] * 100

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{now}] 本月已用 ${cost:.2f} / ${budget:.2f} ({pct:.1f}%)")

    # 告警判断
    if cost >= budget:
        msg = f"🚨 AgentCost 预算已超支！本月 ${cost:.2f} / ${budget:.2f}"
        print(msg)
        # 输出到 stdout 供 cron 投递（no_agent 模式）
        print(f"ALERT: {msg}")
        sys.exit(0)
    elif pct >= threshold:
        msg = f"⚠️ AgentCost 预算提醒：本月已用 ${cost:.2f} ({pct:.1f}%)"
        print(msg)
        print(f"ALERT: {msg}")
        sys.exit(0)
    else:
        print("OK: 预算正常")


if __name__ == "__main__":
    main()
