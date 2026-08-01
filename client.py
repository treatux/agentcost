#!/usr/bin/env python3
"""AgentCost 局域网采集客户端。

扫描本机 Codex、Claude Code 和 Hermes 日志，将用量推送到管理员的
AgentCost 服务器。仅依赖 Python 标准库。

示例：
    python3 client.py --server http://192.168.1.10:8666 \
        --key INGEST_KEY --user alice --once
    python3 client.py --server http://192.168.1.10:8666 \
        --key INGEST_KEY --user alice --interval 300

也可用环境变量 AGENTCOST_SERVER、AGENTCOST_INGEST_KEY、
AGENTCOST_USER 提供 --server、--key、--user。
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import parser
from parser import (
    estimate_cost,
    get_billing_mode,
    get_model_price,
    normalize_usage,
    parse_claude_session,
    parse_codex_session,
    resolve_upstream,
    scan_hermes,
)

DEFAULT_INTERVAL = 300
REQUEST_TIMEOUT = 120


def parse_args():
    """解析命令行与环境变量。"""
    argp = argparse.ArgumentParser(description="采集本机 AI agent 日志并推送到 AgentCost")
    argp.add_argument("--server", default=os.environ.get("AGENTCOST_SERVER"),
                      help="AgentCost 服务器地址（或 AGENTCOST_SERVER）")
    argp.add_argument("--key", default=os.environ.get("AGENTCOST_INGEST_KEY"),
                      help="服务器 ingest_key（或 AGENTCOST_INGEST_KEY）")
    argp.add_argument("--user", default=os.environ.get("AGENTCOST_USER"),
                      help="管理员创建的用户名（或 AGENTCOST_USER）")
    argp.add_argument("--codex-dir", help="Codex 数据目录，覆盖 AGENTCOST_CODEX_DIR")
    argp.add_argument("--claude-dir", help="Claude 数据目录，覆盖 AGENTCOST_CLAUDE_DIR")
    argp.add_argument("--hermes-db", help="Hermes state.db，覆盖 AGENTCOST_HERMES_DB")
    argp.add_argument("--once", action="store_true", help="只扫描并推送一次")
    argp.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                      help="循环模式的扫描间隔（秒，默认 300）")
    args = argp.parse_args()
    missing = [name for name in ("server", "key", "user") if not getattr(args, name)]
    if missing:
        argp.error("缺少必填参数：" + "、".join("--" + name for name in missing))
    if args.interval <= 0:
        argp.error("--interval 必须大于 0")
    return args


def configure_paths(args):
    """将命令行路径覆盖应用到已导入的 parser 模块。"""
    if args.codex_dir:
        parser.CODEX_DIR = os.path.expanduser(args.codex_dir)
    if args.claude_dir:
        parser.CLAUDE_DIR = os.path.expanduser(args.claude_dir)
    if args.hermes_db:
        parser.HERMES_DB = os.path.expanduser(args.hermes_db)


def scan_records():
    """按 parser.scan_all 的来源与规则扫描，但保留来源文件用于幂等 ID。"""
    try:
        parser.load_overrides()
    except Exception:
        pass
    records = []
    for path in glob.glob(os.path.join(parser.CODEX_DIR, "sessions/**/*.jsonl"), recursive=True):
        for record in parse_codex_session(path):
            record["agent"] = "codex"
            record["_source"] = path
            records.append(record)
    for path in glob.glob(os.path.join(parser.CLAUDE_DIR, "projects/**/*.jsonl"), recursive=True):
        for record in parse_claude_session(path):
            record["agent"] = "claude"
            record["base_url"] = ""
            record["billing_mode_raw"] = ""
            record["upstream"] = resolve_upstream(record.get("provider", ""), "", record.get("model") or "")
            record["_source"] = path
            records.append(record)
    for record in scan_hermes():
        record["agent"] = "hermes"
        record["_source"] = parser.HERMES_DB
        records.append(record)
    return records


def token_value(usage, name):
    """安全转换日志中的 token 数。"""
    try:
        return int(usage.get(name) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def ingest_timestamp(value):
    """仅发送服务端可接受的 ISO 时间；异常旧日志交给服务端标记当前时间。"""
    if value is None or value == "":
        return ""
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return str(value)
    except (TypeError, ValueError):
        return ""


def request_id(user, record, usage):
    """生成跨次扫描稳定、不同来源文件也互不冲突的幂等 ID。"""
    parts = (
        user, record.get("agent", ""), record.get("_source", ""), record.get("ts", ""),
        record.get("model", ""), token_value(usage, "input_tokens"),
        token_value(usage, "output_tokens"), token_value(usage, "cache_read_input_tokens"),
        token_value(usage, "cache_creation_input_tokens"), token_value(usage, "reasoning_output_tokens"),
    )
    digest = hashlib.sha1("\x1f".join(map(str, parts)).encode("utf-8")).hexdigest()
    return "%s:%s:%s" % (user, record.get("agent", "unknown"), digest)


def make_ingest_records(user):
    """规范化扫描结果，转换为 /api/ingest 所需字段。"""
    payload = []
    invalid = 0
    for record in scan_records():
        model = str(record.get("model") or "").strip()
        if not model:
            invalid += 1
            continue
        usage = normalize_usage(record.get("usage") or {})
        upstream = record.get("upstream") or resolve_upstream(
            record.get("provider", ""), record.get("base_url", ""), model
        )
        billing_mode = get_billing_mode(model, record.get("provider", ""), upstream)
        cost = record.get("cost_usd")
        if billing_mode == "coding_plan":
            cost = 0
        elif cost is None or cost == 0:
            cost = estimate_cost(model, usage, upstream, billing_mode)
        # 调用价格函数以与 parser 的定价/覆盖规则保持一致；价格由服务端自行保存。
        get_model_price(model, upstream)
        input_tokens = token_value(usage, "input_tokens")
        output_tokens = token_value(usage, "output_tokens")
        cache_read_tokens = token_value(usage, "cache_read_input_tokens")
        cache_write_tokens = token_value(usage, "cache_creation_input_tokens")
        reasoning_tokens = token_value(usage, "reasoning_output_tokens")
        payload.append({
            "agent": record.get("agent", ""), "ts": ingest_timestamp(record.get("ts")), "model": model,
            "provider": record.get("provider", ""), "upstream": upstream,
            "base_url": record.get("base_url", ""), "billing_mode": billing_mode,
            "cwd": record.get("cwd", ""), "originator": record.get("originator", ""),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens, "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
            "cost_usd": cost, "request_id": request_id(user, record, usage), "user": user,
        })
    return payload, invalid


def post_records(server, key, records):
    """以 JSON 数组批量推送记录，并返回服务器响应。"""
    url = server.rstrip("/") + "/api/ingest"
    body = json.dumps(records, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Ingest-Key": key,
    })
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def run_once(args):
    """扫描并推送一轮，返回是否成功。"""
    records, invalid = make_ingest_records(args.user)
    if not records:
        print("没有可推送的记录" + ("（跳过 %d 条缺少模型的记录）" % invalid if invalid else ""))
        return True
    try:
        result = post_records(args.server, args.key, records)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print("推送失败：HTTP %d %s" % (exc.code, detail), file=sys.stderr)
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("推送失败：%s" % exc, file=sys.stderr)
        return False
    if not result.get("ok"):
        print("推送失败：%s" % result.get("error", result), file=sys.stderr)
        return False
    print("推送 %d 条，插入 %d 条，跳过 %d 条重复%s" % (
        len(records), result.get("inserted", 0), result.get("skipped", result.get("duplicates", 0)),
        "；另跳过 %d 条缺少模型的记录" % invalid if invalid else "",
    ))
    return True


def main():
    args = parse_args()
    configure_paths(args)
    while True:
        run_once(args)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
