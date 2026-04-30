"""
US-ETF-Sniper 경량 대시보드 (FastAPI + Jinja2)
- Streamlit 대비 메모리 사용량 1/3~1/4 수준
"""

import os
import re
import glob
import json
import subprocess
import signal
import sys
import pytz
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import time as _time

# 프로젝트 루트 설정
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

print(f"[Dashboard] Starting... BASE_DIR={BASE_DIR}")

# FastAPI 앱 초기화
app = FastAPI(title="US-ETF-Sniper Dashboard")

# 템플릿 설정
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")

# --- Config 관리 ---
CONFIG_FILE = BASE_DIR / "user_config.json"
CACHE_FILE = BASE_DIR / "database/account_cache.json"
STRATEGY_HISTORY_FILE = BASE_DIR / "database/strategy_history.json"
PROFIT_HISTORY_FILE = BASE_DIR / "database/profit_history.json"
ASSET_SNAPSHOTS_FILE = BASE_DIR / "database/asset_snapshots.json"
DESIGN_LIGHT_FILE = BASE_DIR / "data" / "design_w.json"
DESIGN_DARK_FILE = BASE_DIR / "data" / "design_b.json"
ALLOWED_VIEWS = {"overview", "portfolio", "signals", "automation", "logs"}

def load_json_file(file_path, default):
    try:
        if Path(file_path).exists():
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[Dashboard] JSON load error ({file_path}): {e}")
    return default

def load_design_spec(file_path):
    return load_json_file(file_path, {"designSystem": {}})

def load_theme_designs():
    return {
        "light": load_design_spec(DESIGN_LIGHT_FILE),
        "dark": load_design_spec(DESIGN_DARK_FILE),
    }

def resolve_theme_mode(theme_mode):
    return "dark" if str(theme_mode).lower() == "dark" else "light"

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            config.setdefault("theme_mode", "light")
            return config
    return {"trading_mode": "safe", "strategy": "day", "theme_mode": "light"}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                # Debug: Log cache load status
                print(f"[Dashboard] Cache loaded from {CACHE_FILE}, US keys: {list(data.get('us', {}).get('data', {}).keys())}")
                return data
        except Exception as e:
            print(f"[Dashboard] Cache load error: {e}")
            return {}
    else:
        print(f"[Dashboard] Cache file not found: {CACHE_FILE}")
    return {}

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("₩", "").replace("$", "").strip()
            return float(cleaned) if cleaned else default
    except Exception:
        pass
    return default

def safe_int(value, default=0):
    return int(round(safe_float(value, default)))

def iso_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
TRADE_SUCCESS_MARKERS = (
    "buy success",
    "dca buy success",
    "sell success",
    "매수 성공",
    "매도 성공",
)
TRADE_FAILURE_MARKERS = (
    "buy failed",
    "dca buy failed",
    "sell failed",
    "주문 실패",
)
TRADE_ATTEMPT_MARKERS = (
    "dca buy:",
    "buying...",
    "selling market order",
    "selling...",
    "매수:",
    "매도:",
)

def parse_log_timestamp(timestamp_str):
    try:
        return datetime.strptime(timestamp_str, LOG_TIMESTAMP_FORMAT)
    except Exception:
        return None

def format_relative_timestamp(timestamp_str):
    dt_value = parse_log_timestamp(timestamp_str)
    if not dt_value:
        return "시간 미확인"

    delta_seconds = max(int((datetime.now() - dt_value).total_seconds()), 0)
    if delta_seconds < 60:
        return "방금 전"

    delta_minutes = delta_seconds // 60
    if delta_minutes < 60:
        return f"{delta_minutes}분 전"

    delta_hours = delta_minutes // 60
    if delta_hours < 24:
        return f"{delta_hours}시간 전"

    delta_days = delta_hours // 24
    return f"{delta_days}일 전"

def read_log_lines(file_path, limit=240):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.readlines()[-limit:]
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="cp949") as f:
            return f.readlines()[-limit:]
    except Exception:
        return []

def load_recent_log_events(limit_files=14, line_limit=240):
    log_files = sorted(glob.glob(str(BASE_DIR / "database" / "trading_*.log")))[-limit_files:]
    events = []
    for file_path in log_files:
        for raw_line in read_log_lines(file_path, limit=line_limit):
            parsed = parse_log_line(raw_line)
            if parsed:
                events.append(parsed)
    return events

def extract_symbol_from_message(message):
    match = re.search(r"\[([A-Z0-9]+)\]", str(message or ""))
    return match.group(1) if match else None

def normalize_activity_event(log, status, tone=None):
    if not log:
        return None

    message = log.get("message", "")
    return {
        "timestamp": log.get("timestamp", "-"),
        "age": format_relative_timestamp(log.get("timestamp")),
        "symbol": extract_symbol_from_message(message) or "-",
        "message": message,
        "status": status,
        "tone": tone or ("positive" if status == "success" else "negative" if status == "failure" else "info"),
    }

def build_activity_snapshot(parsed_logs, strategy_timeline):
    all_events = load_recent_log_events()

    def find_latest(predicate):
        for log in reversed(all_events):
            if predicate(log):
                return log
        return None

    def has_marker(log, markers):
        message = str(log.get("message", "")).lower()
        return any(marker in message for marker in markers)

    last_heartbeat_log = find_latest(lambda log: "heartbeat:" in str(log.get("message", "")).lower())
    last_cache_log = find_latest(lambda log: "account cache updated" in str(log.get("message", "")).lower())
    last_trade_success_log = find_latest(lambda log: has_marker(log, TRADE_SUCCESS_MARKERS))
    last_trade_failure_log = find_latest(lambda log: has_marker(log, TRADE_FAILURE_MARKERS))
    last_trade_attempt_log = find_latest(lambda log: has_marker(log, TRADE_ATTEMPT_MARKERS))

    candidate_orders = [
        normalize_activity_event(last_trade_success_log, "success", "positive"),
        normalize_activity_event(last_trade_failure_log, "failure", "negative"),
        normalize_activity_event(last_trade_attempt_log, "attempt", "info"),
    ]
    candidate_orders = [item for item in candidate_orders if item]
    candidate_orders.sort(key=lambda item: parse_log_timestamp(item["timestamp"]) or datetime.min, reverse=True)
    last_order = candidate_orders[0] if candidate_orders else None

    freshness_anchor = last_heartbeat_log or last_cache_log or (parsed_logs[-1] if parsed_logs else None)
    freshness_label = format_relative_timestamp(freshness_anchor["timestamp"]) if freshness_anchor else "기록 없음"

    last_strategy_change = strategy_timeline[0] if strategy_timeline else None

    return {
        "lastHeartbeat": normalize_activity_event(last_heartbeat_log, "heartbeat", "positive"),
        "lastCacheUpdate": normalize_activity_event(last_cache_log, "cache", "info"),
        "lastTradeSuccess": normalize_activity_event(last_trade_success_log, "success", "positive"),
        "lastTradeFailure": normalize_activity_event(last_trade_failure_log, "failure", "negative"),
        "lastTradeAttempt": normalize_activity_event(last_trade_attempt_log, "attempt", "info"),
        "lastOrder": last_order,
        "freshnessLabel": freshness_label,
        "lastStrategyChange": {
            "timestamp": last_strategy_change["timestamp"],
            "age": format_relative_timestamp(last_strategy_change["timestamp"]),
            "summary": last_strategy_change["reason"],
            "market": last_strategy_change["market"],
            "strategy": last_strategy_change["strategy"],
            "mode": last_strategy_change["mode"],
        } if last_strategy_change else None,
    }

def get_recent_logs(limit=120):
    log_file = get_latest_log_file()
    parsed_lines = []
    last_update = "N/A"

    if log_file:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        except UnicodeDecodeError:
            with open(log_file, "r", encoding="cp949") as f:
                lines = f.readlines()[-limit:]
        except Exception:
            lines = []

        parsed_lines = [parse_log_line(line) for line in lines]
        parsed_lines = [line for line in parsed_lines if line is not None]
        if parsed_lines:
            last_update = parsed_lines[-1]["timestamp"]

    return parsed_lines, last_update

def build_views():
    return [
        {"id": "overview", "label": "개요", "description": "실시간 운영 현황"},
        {"id": "portfolio", "label": "포트폴리오", "description": "미국/국내 보유 자산"},
        {"id": "signals", "label": "시그널", "description": "티커 감시 및 전략 상태"},
        {"id": "automation", "label": "자동화", "description": "봇 설정과 리스크 제어"},
        {"id": "logs", "label": "운영 로그", "description": "실행 로그와 이벤트 타임라인"},
    ]

def build_user_session():
    return {
        "user": {
            "id": "operator-001",
            "name": "Operator",
            "role": "admin",
            "avatarInitials": "OP",
        },
        "permissions": [view["id"] for view in build_views()],
    }

def get_symbol_accent(symbol):
    palette = ["#F5C75B", "#4C8DFF", "#1FCB7B", "#EA4E5A", "#8B5CF6", "#F7931A"]
    if not symbol:
        return palette[0]
    return palette[sum(ord(ch) for ch in str(symbol)) % len(palette)]

def build_holdings_inventory(us_account, kr_account):
    holdings = []
    exchange_rate = safe_float(us_account.get("exchange_rate"), USD_KRW_RATE) or USD_KRW_RATE

    for item in us_account.get("holdings", []):
        symbol = item.get("ticker", "N/A")
        qty = safe_float(item.get("qty"))
        current_price = safe_float(item.get("cur_price"))
        eval_amount = safe_float(item.get("eval_amt")) or (qty * current_price)
        holdings.append({
            "id": f"holding-us-{symbol}",
            "region": "US",
            "symbol": symbol,
            "name": item.get("name", symbol),
            "qty": qty,
            "avgPrice": safe_float(item.get("avg_price")),
            "currentPrice": current_price,
            "evalAmount": eval_amount,
            "evalAmountKrw": eval_amount * exchange_rate,
            "profit": safe_float(item.get("profit")),
            "profitPct": safe_float(item.get("profit_pct")),
            "accentColor": get_symbol_accent(symbol),
        })

    for item in kr_account.get("holdings", []):
        symbol = item.get("code", "N/A")
        qty = safe_float(item.get("qty"))
        current_price = safe_float(item.get("cur_price"))
        eval_amount_krw = qty * current_price
        holdings.append({
            "id": f"holding-kr-{symbol}",
            "region": "KR",
            "symbol": symbol,
            "name": item.get("name", symbol),
            "qty": qty,
            "avgPrice": safe_float(item.get("avg_price")),
            "currentPrice": current_price,
            "evalAmount": eval_amount_krw,
            "evalAmountKrw": eval_amount_krw,
            "profit": safe_float(item.get("profit")),
            "profitPct": safe_float(item.get("profit_pct")),
            "accentColor": get_symbol_accent(symbol),
        })

    return sorted(holdings, key=lambda row: row.get("evalAmountKrw", 0), reverse=True)

def build_allocation(holdings):
    total = sum(item.get("evalAmountKrw", 0) for item in holdings) or 1
    return [
        {
            "id": f"allocation-{item['id']}",
            "label": item["symbol"],
            "name": item["name"],
            "value": item["evalAmountKrw"],
            "share": round((item["evalAmountKrw"] / total) * 100, 2),
            "color": item["accentColor"],
        }
        for item in holdings[:8]
    ]

def build_signal_items(ticker_data, holdings):
    signal_items = []
    holdings_map = {item["symbol"]: item for item in holdings}
    all_symbols = sorted(set(list(ticker_data.keys()) + list(holdings_map.keys())))

    for symbol in all_symbols:
        raw = ticker_data.get(symbol, {})
        current = safe_float(raw.get("current"))
        ma20 = safe_float(raw.get("ma20"))
        target = safe_float(raw.get("target"))
        trend = raw.get("trend", "감시")
        delta_pct = round(((current - ma20) / ma20) * 100, 2) if current and ma20 else 0.0
        strength = max(28, min(96, int(55 + (delta_pct * 7)))) if ma20 else (68 if "Bull" in trend else 46)
        owned = symbol in holdings_map
        region = holdings_map.get(symbol, {}).get("region", "WATCH")

        signal_items.append({
            "id": f"signal-{symbol}",
            "symbol": symbol,
            "name": holdings_map.get(symbol, {}).get("name", symbol),
            "region": region,
            "current": current,
            "ma20": ma20,
            "target": target,
            "trend": trend,
            "deltaPct": delta_pct,
            "strength": strength,
            "owned": owned,
            "action": "매수 감시" if strength >= 60 else "리스크 방어",
            "accentColor": holdings_map.get(symbol, {}).get("accentColor", get_symbol_accent(symbol)),
        })

    return sorted(signal_items, key=lambda row: (row["owned"], row["strength"]), reverse=True)

def build_strategy_timeline(changes):
    timeline = []
    for index, change in enumerate(reversed(changes[-8:])):
        new_cfg = change.get("new", {})
        timeline.append({
            "id": f"strategy-{index}",
            "timestamp": change.get("timestamp", "N/A"),
            "market": change.get("market", "-"),
            "strategy": new_cfg.get("strategy", "-"),
            "mode": new_cfg.get("mode", "-"),
            "persona": new_cfg.get("persona", "-"),
            "reason": change.get("reason", "전략 변경 기록"),
            "confidence": safe_float(change.get("confidence"), 0.0),
        })
    return timeline

def build_profit_days(profit_history):
    days = []
    for key in sorted(profit_history.keys(), reverse=True)[:20]:
        row = profit_history.get(key, {})
        days.append({
            "id": f"profit-{key}",
            "date": key,
            "market": row.get("market", "-"),
            "realizedProfit": safe_float(row.get("realized_profit"), 0.0),
            "tradeCount": len(row.get("trades", [])),
        })
    return days

def build_asset_trend(asset_snapshots, combined_total_krw):
    points = []
    for key in sorted(asset_snapshots.keys()):
        snapshot = asset_snapshots.get(key, {})
        points.append({
            "id": f"snapshot-{key}",
            "label": key[5:].replace("-", "/"),
            "date": key,
            "value": safe_float(snapshot.get("total_krw", snapshot.get("combined_krw", 0))),
            "usUsd": safe_float(snapshot.get("us", {}).get("eval_total_usd", snapshot.get("us_total_usd", 0))),
            "krKrw": safe_float(snapshot.get("kr", {}).get("eval_total", snapshot.get("kr_total_krw", 0))),
        })

    today = datetime.now().strftime("%Y-%m-%d")
    if not points or points[-1]["date"] != today:
        points.append({
            "id": f"snapshot-{today}",
            "label": "오늘",
            "date": today,
            "value": combined_total_krw,
            "usUsd": 0.0,
            "krKrw": 0.0,
        })

    if len(points) == 1:
        baseline = max(points[0]["value"] * 0.97, 1)
        points.insert(0, {
            "id": "snapshot-baseline",
            "label": "직전",
            "date": today,
            "value": baseline,
            "usUsd": 0.0,
            "krKrw": 0.0,
        })

    return points[-24:]

def build_stories(strategy_timeline, parsed_logs, config, market_status, activity_snapshot):
    stories = []

    if strategy_timeline:
        latest = strategy_timeline[0]
        stories.append({
            "id": "story-strategy-latest",
            "badge": "전략",
            "tone": "accent",
            "title": f"{latest['market']} 시장 자동 전략 업데이트",
            "summary": latest["reason"],
            "meta": latest["timestamp"],
        })

    last_order = activity_snapshot.get("lastOrder") if activity_snapshot else None
    if last_order:
        status_label = {
            "success": "체결",
            "failure": "실패",
            "attempt": "주문",
        }.get(last_order["status"], "활동")
        stories.append({
            "id": "story-last-order",
            "badge": status_label,
            "tone": last_order["tone"],
            "title": f"최근 주문 · {last_order.get('symbol', '-')}",
            "summary": last_order["message"],
            "meta": f"{last_order['timestamp']} · {last_order['age']}",
        })

    warning_logs = [log for log in reversed(parsed_logs) if log["level"] in {"WARNING", "ERROR"}][:2]
    for index, log in enumerate(warning_logs):
        stories.append({
            "id": f"story-log-{index}",
            "badge": log["level"],
            "tone": "negative" if log["level"] == "ERROR" else "warning",
            "title": "운영 알림",
            "summary": log["message"],
            "meta": log["timestamp"],
        })

    stories.append({
        "id": "story-market-mode",
        "badge": market_status,
        "tone": "info",
        "title": "현재 실행 컨텍스트",
        "summary": f"자동 전략 {'활성화' if config.get('auto_strategy') else '비활성화'} · {config.get('strategy', 'day').upper()} / {config.get('trading_mode', 'safe').upper()} / {config.get('persona', 'neutral').upper()}",
        "meta": iso_now(),
    })

    return stories[:4]

def build_alerts(bot_pid, market_status, config, us_account, kr_account, signal_items, activity_snapshot):
    strongest_signal = signal_items[0] if signal_items else None
    last_order = activity_snapshot.get("lastOrder") if activity_snapshot else None
    alerts = [
        {
            "id": "alert-bot-status",
            "title": "봇 상태",
            "text": f"{'실행 중' if bot_pid else '중지됨'} · 시장 {market_status} · 최신 활동 {activity_snapshot.get('freshnessLabel', '기록 없음') if activity_snapshot else '기록 없음'}",
            "severity": "positive" if bot_pid else "negative",
            "value": f"PID {bot_pid}" if bot_pid else "대기",
        },
        {
            "id": "alert-auto-strategy",
            "title": "자동 전략",
            "text": "시장 상황 기반으로 전략/모드/페르소나를 동기화합니다." if config.get("auto_strategy") else "수동 제어 중입니다. 변경 후 재시작이 필요합니다.",
            "severity": "accent" if config.get("auto_strategy") else "warning",
            "value": "ON" if config.get("auto_strategy") else "OFF",
        },
        {
            "id": "alert-cache-status",
            "title": "계좌 캐시",
            "text": f"US {us_account.get('_cache_age', 'N/A')} · KR {kr_account.get('_cache_age', 'N/A')}",
            "severity": "info",
            "value": "LIVE" if us_account.get("_source") == "live" or kr_account.get("_source") == "live" else "CACHE",
        },
    ]

    if last_order:
        status_label = {
            "success": "체결",
            "failure": "실패",
            "attempt": "주문",
        }.get(last_order["status"], "활동")
        alerts.append({
            "id": "alert-last-order",
            "title": "최근 주문",
            "text": f"{last_order.get('symbol', '-')} · {last_order['message']}",
            "severity": last_order["tone"],
            "value": f"{status_label} · {last_order['age']}",
        })

    if strongest_signal:
        alerts.append({
            "id": "alert-top-signal",
            "title": "최우선 감시 종목",
            "text": f"{strongest_signal['symbol']} · {strongest_signal['action']} · 강도 {strongest_signal['strength']}",
            "severity": "accent" if strongest_signal["strength"] >= 60 else "warning",
            "value": strongest_signal["trend"],
        })

    return alerts

def build_market_ticker(accounts, status, config, signal_items, activity_snapshot):
    last_order = activity_snapshot.get("lastOrder") if activity_snapshot else None
    combined = accounts["combined"]
    return [
        {
            "id": "ticker-total",
            "label": "총 자산",
            "value": f"₩{combined['totalKrw']:,}",
            "trend": f"{combined['profitPct']:+.2f}%",
            "tone": "positive" if combined["profitPct"] >= 0 else "negative",
        },
        {
            "id": "ticker-market",
            "label": "시장 상태",
            "value": status["marketStatus"],
            "trend": activity_snapshot.get("freshnessLabel", status["botStatusLabel"]) if activity_snapshot else status["botStatusLabel"],
            "tone": "positive" if status["botRunning"] else "negative",
        },
        {
            "id": "ticker-config",
            "label": "운영 설정",
            "value": f"{config.get('strategy', 'day').upper()} / {config.get('trading_mode', 'safe').upper()}",
            "trend": config.get("persona", "neutral").upper(),
            "tone": "info",
        },
        {
            "id": "ticker-last-order",
            "label": "최근 주문",
            "value": last_order["symbol"] if last_order else "없음",
            "trend": (last_order["age"] if last_order else "기록 없음"),
            "tone": last_order["tone"] if last_order else "warning",
        },
    ]

def build_dashboard_payload(force_update=False):
    config = load_config()
    config["theme_mode"] = resolve_theme_mode(config.get("theme_mode", "light"))
    bot_pid = get_bot_pid()
    market_status = get_market_status()
    parsed_logs, last_update = get_recent_logs(limit=160)
    ticker_data = parse_ticker_data(parsed_logs)

    us_account = get_account_data(force_update)
    kr_account = get_kr_account_data(force_update)
    strategy_data = load_json_file(STRATEGY_HISTORY_FILE, {"changes": []})
    strategy_timeline = build_strategy_timeline(strategy_data.get("changes", []))
    activity_snapshot = build_activity_snapshot(parsed_logs, strategy_timeline)
    profit_history = load_json_file(PROFIT_HISTORY_FILE, {})
    asset_snapshots = load_json_file(ASSET_SNAPSHOTS_FILE, {})

    combined_total_krw = safe_int(us_account.get("total_asset_krw")) + safe_int(kr_account.get("total_asset_krw"))
    combined_profit_krw = safe_int(us_account.get("profit_krw")) + safe_int(kr_account.get("profit_krw"))
    exchange_rate = safe_float(us_account.get("exchange_rate"), USD_KRW_RATE) or USD_KRW_RATE
    combined_total_usd = round(safe_float(us_account.get("total_asset_usd")) + (safe_float(kr_account.get("total_asset_krw")) / exchange_rate), 2)
    combined_profit_usd = round(safe_float(us_account.get("profit_usd")) + (safe_float(kr_account.get("profit_krw")) / exchange_rate), 2)
    invested_basis = max(combined_total_krw - combined_profit_krw, 1)
    combined_profit_pct = round((combined_profit_krw / invested_basis) * 100, 2)

    holdings = build_holdings_inventory(us_account, kr_account)
    allocation = build_allocation(holdings)
    signals = build_signal_items(ticker_data, holdings)

    accounts = {
        "us": us_account,
        "kr": kr_account,
        "combined": {
            "totalKrw": combined_total_krw,
            "totalUsd": combined_total_usd,
            "profitKrw": combined_profit_krw,
            "profitUsd": combined_profit_usd,
            "profitPct": combined_profit_pct,
            "exchangeRate": exchange_rate,
        },
    }

    status = {
        "botRunning": bool(bot_pid),
        "botPid": bot_pid,
        "botStatusLabel": "Running" if bot_pid else "Stopped",
        "marketStatus": market_status,
        "lastUpdate": last_update,
        "generatedAt": iso_now(),
        "autoStrategy": bool(config.get("auto_strategy", False)),
    }

    return {
        "generatedAt": iso_now(),
        "session": build_user_session(),
        "views": build_views(),
        "status": status,
        "activity": activity_snapshot,
        "config": config,
        "accounts": accounts,
        "holdings": holdings,
        "signals": signals,
        "stories": build_stories(strategy_timeline, parsed_logs, config, market_status, activity_snapshot),
        "alerts": build_alerts(bot_pid, market_status, config, us_account, kr_account, signals, activity_snapshot),
        "marketTicker": build_market_ticker(accounts, status, config, signals, activity_snapshot),
        "logs": [
            {
                "id": f"log-{index}",
                "timestamp": log["timestamp"],
                "level": log["level"],
                "message": log["message"],
            }
            for index, log in enumerate(reversed(parsed_logs[-40:]))
        ],
        "history": {
            "strategy": strategy_timeline,
            "profitDays": build_profit_days(profit_history),
            "snapshots": build_asset_trend(asset_snapshots, combined_total_krw),
        },
        "charts": {
            "assetTrend": build_asset_trend(asset_snapshots, combined_total_krw),
            "allocation": allocation,
            "profitDistribution": [
                {
                    "id": f"profit-{item['id']}",
                    "label": item["symbol"],
                    "value": item["profitPct"],
                    "color": item["accentColor"],
                }
                for item in holdings[:8]
            ],
        },
        "preferences": {
            "dca": config.get("dca_settings", {}),
            "risk": config.get("risk_management", {}),
            "telegram": config.get("telegram", {}),
        },
        "theme": {
            "current": config.get("theme_mode", "light"),
            "available": ["light", "dark"],
        },
    }

def resolve_initial_view(view_path):
    candidate = (view_path or "overview").strip("/").split("/")[0] or "overview"
    return candidate if candidate in ALLOWED_VIEWS else "overview"

def render_dashboard_response(request: Request, initial_view="overview"):
    theme_mode = resolve_theme_mode(load_config().get("theme_mode", "light"))
    theme_designs = load_theme_designs()
    return templates.TemplateResponse("dashboard_v2.html", {
        "request": request,
        "active_design": theme_designs.get(theme_mode, theme_designs["light"]),
        "theme_designs": theme_designs,
        "theme_mode": theme_mode,
        "initial_state": build_dashboard_payload(),
        "initial_view": initial_view,
    })

# --- 유틸리티 함수 ---
def get_bot_pid():
    """봇 프로세스 ID 조회"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_bot.py"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            if pids and pids[0]:
                return int(pids[0])
    except Exception:
        pass
    return None

def get_latest_log_file():
    """최신 로그 파일 경로"""
    log_files = glob.glob(str(BASE_DIR / "database" / "trading_*.log"))
    if not log_files:
        return None
    return sorted(log_files)[-1]

def parse_log_line(line):
    """로그 라인 파싱"""
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - (\w+) - (.*)", line)
    if match:
        return {
            "timestamp": match.group(1),
            "level": match.group(2),
            "message": match.group(3)
        }
    return None

def get_market_status():
    """현재 시장 상태 (KST 기준)"""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)
    t = int(now.strftime("%H%M"))
    
    if 2330 <= t <= 2400 or 0 <= t < 600:
        return 'US'
    if 900 <= t <= 1520:
        return 'KR'
    return 'CLOSED'

def parse_ticker_data(parsed_lines):
    """로그에서 티커별 데이터 추출"""
    ticker_data = {}
    
    for line in parsed_lines:
        msg = line['message']
        ticker_match = re.search(r"\[([A-Z0-9]+)\]", msg)
        if not ticker_match:
            continue
        
        ticker = ticker_match.group(1)
        if ticker not in ticker_data:
            ticker_data[ticker] = {
                "current": "N/A", "ma20": "N/A", 
                "target": "N/A", "trend": "Unknown"
            }
        
        # Current & MA20
        m1 = re.search(r"Current: ([^,]+), MA20: (.+)", msg)
        if m1:
            ticker_data[ticker]["current"] = m1.group(1).strip()
            ticker_data[ticker]["ma20"] = m1.group(2).strip()
        
        # Target Price (Bull)
        m2 = re.search(r"Target Price: ([^ ]+)", msg)
        if m2:
            ticker_data[ticker]["target"] = m2.group(1).strip()
            ticker_data[ticker]["trend"] = "Bull 🐂"
        
        # Bear Market
        if "Bear Market" in msg:
            ticker_data[ticker]["trend"] = "Bear 🐻"
            ticker_data[ticker]["target"] = "-"
    
    return ticker_data

# 환율 설정
USD_KRW_RATE = 1450.0

# 캐시 최대 유효 시간 (초) - 이보다 오래된 캐시는 자동 갱신 시도
CACHE_MAX_AGE_SECONDS = 300  # 5분

def _is_cache_stale(cache_entry, max_age=CACHE_MAX_AGE_SECONDS):
    """캐시가 max_age초 이상 오래되었는지 확인"""
    ts = cache_entry.get("timestamp", 0)
    if ts == 0:
        return True
    return (_time.time() - ts) > max_age

def _get_cache_age_str(cache_entry):
    """캐시 나이를 사람이 읽기 쉬운 문자열로 반환"""
    ts = cache_entry.get("timestamp", 0)
    if ts == 0:
        return "알 수 없음"
    age = _time.time() - ts
    if age < 60:
        return f"{int(age)}초 전"
    elif age < 3600:
        return f"{int(age / 60)}분 전"
    elif age < 86400:
        return f"{int(age / 3600)}시간 전"
    else:
        return f"{int(age / 86400)}일 전"

def _refresh_us_live():
    """US 계좌를 API에서 실시간 조회하여 캐시 갱신"""
    try:
        from modules.account_manager import update_us_account
        result = update_us_account()
        if result:
            print(f"[Dashboard] US Account LIVE refresh OK: deposit=${result.get('deposit_usd')}")
            return result
        print("[Dashboard] US Account LIVE refresh returned None")
    except Exception as e:
        print(f"[Dashboard] US Account LIVE refresh FAILED: {e}")
    return None

def _refresh_kr_live():
    """KR 계좌를 API에서 실시간 조회하여 캐시 갱신"""
    try:
        from modules.account_manager import update_kr_account
        result = update_kr_account()
        if result:
            print(f"[Dashboard] KR Account LIVE refresh OK: deposit={result.get('deposit_krw')}")
            return result
        print("[Dashboard] KR Account LIVE refresh returned None")
    except Exception as e:
        print(f"[Dashboard] KR Account LIVE refresh FAILED: {e}")
    return None

def get_account_data(force_update=False):
    """US 계좌 정보 조회 (필요 시 실시간 갱신)"""
    cache = load_cache()
    us_entry = cache.get("us", {})
    us_data = us_entry.get("data")
    cache_age = _get_cache_age_str(us_entry)
    
    # force 요청이거나 캐시가 오래된 경우 실시간 조회
    if force_update or _is_cache_stale(us_entry):
        print(f"[Dashboard] US Account cache stale ({cache_age}), refreshing live...")
        live_data = _refresh_us_live()
        if live_data:
            live_data["_cache_age"] = "방금 갱신"
            live_data["_source"] = "live"
            return live_data
        # 실패 시 캐시 fallback
        print("[Dashboard] US live refresh failed, using stale cache")
    
    if us_data:
        us_data["_cache_age"] = cache_age
        us_data["_source"] = "cache"
        return us_data
    
    print("[Dashboard] US Account data not found")
    return {
        "deposit_usd": 0.0, "deposit_krw": 0, "total_asset_usd": 0.0, "total_asset_krw": 0,
        "profit_usd": 0.0, "profit_krw": 0, "exchange_rate": USD_KRW_RATE, "holdings": [],
        "_cache_age": "데이터 없음", "_source": "none",
        "msg": "Waiting for data..."
    }

def get_kr_account_data(force_update=False):
    """KR 계좌 정보 조회 (필요 시 실시간 갱신)"""
    cache = load_cache()
    kr_entry = cache.get("kr", {})
    kr_data = kr_entry.get("data")
    cache_age = _get_cache_age_str(kr_entry)
    
    # force 요청이거나 캐시가 오래된 경우 실시간 조회
    if force_update or _is_cache_stale(kr_entry):
        print(f"[Dashboard] KR Account cache stale ({cache_age}), refreshing live...")
        live_data = _refresh_kr_live()
        if live_data:
            live_data["_cache_age"] = "방금 갱신"
            live_data["_source"] = "live"
            return live_data
        print("[Dashboard] KR live refresh failed, using stale cache")
    
    if kr_data:
        kr_data["_cache_age"] = cache_age
        kr_data["_source"] = "cache"
        return kr_data
    
    print("[Dashboard] KR Account data not found")
    return {
        "deposit_krw": "0", "total_asset_krw": "0", "profit_krw": "0", "holdings": [],
        "_cache_age": "데이터 없음", "_source": "none",
        "msg": "Waiting for data..."
    }

# --- API 엔드포인트 ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """메인 대시보드 페이지"""
    return render_dashboard_response(request, "overview")

@app.get("/api/status")
async def api_status():
    """봇 상태 API"""
    bot_pid = get_bot_pid()
    market_status = get_market_status()
    log_file = get_latest_log_file()
    parsed_lines = []
    last_update = "N/A"
    
    if log_file:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-200:]
        except:
            lines = []
            
        parsed_lines = [parse_log_line(line) for line in lines]
        parsed_lines = [x for x in parsed_lines if x is not None]
        if parsed_lines:
            last_update = parsed_lines[-1]['timestamp']
            
    ticker_data = parse_ticker_data(parsed_lines)
    
    return JSONResponse({
        "bot_pid": bot_pid,
        "bot_status": "🟢 Running" if bot_pid else "🔴 Stopped",
        "market_status": market_status,
        "last_update": last_update,
        "ticker_data": ticker_data,
        "recent_logs": parsed_lines[-20:][::-1],
        "config": load_config(),
    })

@app.get("/api/account")
async def api_account(force: bool = False):
    data = get_account_data(force)
    return JSONResponse(data)

@app.get("/api/account/kr")
async def api_account_kr(force: bool = False):
    return JSONResponse(get_kr_account_data(force))

@app.get("/api/dashboard-data")
async def api_dashboard_data(force: bool = False):
    return JSONResponse(build_dashboard_payload(force))

@app.post("/api/config")
async def update_config(request: Request):
    data = await request.json()
    config = load_config()
    if "auto_strategy" in data:
        config["auto_strategy"] = data["auto_strategy"]
    if "trading_mode" in data:
        config["trading_mode"] = data["trading_mode"]
    if "strategy" in data:
        config["strategy"] = data["strategy"]
    if "persona" in data:
        config["persona"] = data["persona"]
    if "theme_mode" in data:
        config["theme_mode"] = resolve_theme_mode(data["theme_mode"])
    save_config(config)
    return JSONResponse({"success": True, "config": config})

@app.get("/api/strategy-history")
async def api_strategy_history():
    """전략 변경 히스토리 API"""
    try:
        if STRATEGY_HISTORY_FILE.exists():
            with open(STRATEGY_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                changes = data.get("changes", [])
                # 최근 10개만
                return JSONResponse({"changes": changes[-10:][::-1]})
    except Exception as e:
        print(f"[Dashboard] Strategy history error: {e}")
    return JSONResponse({"changes": []})

@app.get("/api/profit-summary")
async def api_profit_summary():
    """수익 요약 API"""
    result = {"profit_history": {}, "asset_snapshots": {}}
    try:
        if PROFIT_HISTORY_FILE.exists():
            with open(PROFIT_HISTORY_FILE, "r", encoding="utf-8") as f:
                result["profit_history"] = json.load(f)
    except Exception:
        pass
    try:
        if ASSET_SNAPSHOTS_FILE.exists():
            with open(ASSET_SNAPSHOTS_FILE, "r", encoding="utf-8") as f:
                result["asset_snapshots"] = json.load(f)
    except Exception:
        pass
    return JSONResponse(result)

@app.post("/api/restart")
async def restart_bot():
    old_pid = get_bot_pid()
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            import time
            time.sleep(1)
            if get_bot_pid():
                os.kill(old_pid, signal.SIGKILL)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)})
    
    try:
        cmd = f"cd {BASE_DIR} && source venv/bin/activate && nohup python run_bot.py >> database/bot_stdout.log 2>&1 &"
        subprocess.Popen(cmd, shell=True, executable="/bin/bash")
        return JSONResponse({"success": True, "message": "Bot restarted"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

@app.get("/{view_path:path}", response_class=HTMLResponse)
async def dashboard_view(request: Request, view_path: str):
    if view_path.startswith("api/") or view_path == "api":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return render_dashboard_response(request, resolve_initial_view(view_path))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501)
