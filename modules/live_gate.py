"""Live-order safety checks (automatic — no operator approval).

With `EXECUTION_MODE=live` the bot sends real orders on its own. This module only
blocks them when an automatic check fails: the HMAC-signed read-only preflight
that every deploy writes is missing, or the emergency stop is on
(`database/live_halt` / LIVE_TRADING_HALT=1). The optional one-share smoke tool
(`scripts/live_smoke.py`) still uses a signed one-time authorisation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from modules.brokers.models import BrokerError, Market

STATE_DIR = Path("database")
PREFLIGHT_FILE = STATE_DIR / "live_preflight.json"
SMOKE_FILE = STATE_DIR / "live_smoke.json"
SMOKE_AUTH_FILE = STATE_DIR / "live_smoke_authorization.json"
HEALTH_FILE = STATE_DIR / "strategy_health.json"

PREFLIGHT_MAX_AGE_SEC = 60 * 60
SMOKE_MAX_AGE_SEC = 7 * 24 * 60 * 60
DEFAULT_REQUIRED_SHADOW_SESSIONS = 20


def _secret() -> bytes:
    value = os.getenv("LIVE_GATE_SECRET", "").strip()
    if not value:
        raise BrokerError("LIVE_GATE_SECRET이 없어 live 주문 게이트를 통과할 수 없습니다", code="live-gate-secret-missing")
    return value.encode("utf-8")


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(payload: Dict[str, Any]) -> str:
    return hmac.new(_secret(), _canonical(payload), hashlib.sha256).hexdigest()


def _write_signed(path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["signature"] = _sign(body)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return body


def _read_signed(path: Path) -> Dict[str, Any]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        signature = body.pop("signature")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BrokerError(f"live 게이트 기록을 읽을 수 없습니다: {path.name}", code="live-gate-record-missing") from exc
    expected = _sign(body)
    if not hmac.compare_digest(str(signature), expected):
        raise BrokerError(f"live 게이트 기록 서명이 일치하지 않습니다: {path.name}", code="live-gate-signature-invalid")
    return body


def _fresh(record: Dict[str, Any], limit_sec: int, code: str, label: str) -> None:
    age = time.time() - float(record.get("created_at", 0))
    if age < 0 or age > limit_sec:
        raise BrokerError(f"{label}이 만료되었습니다 ({max(age, 0):.0f}초 경과)", code=code)


def _health_sessions() -> List[Dict[str, Any]]:
    try:
        return list(json.loads(HEALTH_FILE.read_text(encoding="utf-8")).get("sessions") or [])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def shadow_evidence(required: int = DEFAULT_REQUIRED_SHADOW_SESSIONS) -> Dict[str, int]:
    """Count completed, error-free shadow rotation sessions by market.

    Only records emitted after this gate was introduced count; older records
    lack explicit mode/preflight provenance and are not evidence for live.
    """
    needed = max(1, int(required))
    seen = {"KR": set(), "US": set()}
    for row in _health_sessions():
        market = str(row.get("market", "")).upper()
        notes = str(row.get("notes", ""))
        if market not in seen or "strategy=momentum_rotation" not in notes:
            continue
        if "execution_mode=shadow" not in notes or "preflight=ok" not in notes:
            continue
        if row.get("session_key"):
            seen[market].add(str(row["session_key"]))
    return {market: len(keys) for market, keys in seen.items()}


def preflight_verified() -> bool:
    """Whether a signed, successful baseline preflight record exists."""
    try:
        record = _read_signed(PREFLIGHT_FILE)
        return set(record.get("markets") or []) == {"KR", "US"}
    except BrokerError:
        return False


def preflight_ok() -> bool:
    """True only while the latest signed read-only broker preflight is fresh."""
    try:
        record = _read_signed(PREFLIGHT_FILE)
        _fresh(record, PREFLIGHT_MAX_AGE_SEC, "live-preflight-stale", "read-only preflight")
        return set(record.get("markets") or []) == {"KR", "US"}
    except BrokerError:
        return False


def trading_halted() -> str:
    """Emergency stop. Returns the reason when live orders must not be sent.

    `database/live_halt` (any content) or LIVE_TRADING_HALT=1 stops every new live
    order without a redeploy. Remove the file to resume. Switching
    EXECUTION_MODE back to shadow is the other way to stop.
    """
    if os.getenv("LIVE_TRADING_HALT", "").strip().lower() in ("1", "true", "yes", "on"):
        return "LIVE_TRADING_HALT 환경변수"
    halt = STATE_DIR / "live_halt"
    if halt.exists():
        try:
            note = halt.read_text(encoding="utf-8").strip()
        except OSError:
            note = ""
        return f"{halt} 파일" + (f" ({note[:80]})" if note else "")
    return ""


def gate_status(required: int = DEFAULT_REQUIRED_SHADOW_SESSIONS) -> Dict[str, Any]:
    """Non-throwing operator status for the dashboard.

    Live orders need only an automatic, signed read-only preflight (written by
    every deploy) and no emergency stop. Shadow session counts are informational.
    """
    required = max(1, int(required))
    evidence = shadow_evidence(required)
    verified = preflight_verified()
    halted = trading_halted()
    return {
        "required_shadow_sessions": required,
        "shadow_sessions": evidence,
        "preflight": {"ok": verified, "markets": ["KR", "US"] if verified else []},
        "halted": bool(halted),
        "halt_reason": halted,
        "markets": {
            market: {
                "shadow_ok": evidence[market] >= required,
                "shadow_remaining": max(0, required - evidence[market]),
                "preflight_ok": verified,
                "live_allowed": verified and not halted,
            }
            for market in ("KR", "US")
        },
    }


def assert_live_order_allowed(market: Market) -> None:
    """Hard broker-side guard for every non-smoke live order.

    No human approval step: the bot trades autonomously once deployed with
    EXECUTION_MODE=live. Blocks only on automatic safety conditions.
    """
    halted = trading_halted()
    if halted:
        raise BrokerError(f"live 주문 비상정지 중: {halted}", code="live-trading-halted")
    if not preflight_verified():
        raise BrokerError("배포 시 read-only preflight 기록이 없어 live 주문을 보낼 수 없습니다",
                          code="live-preflight-missing")


def record_preflight(markets: Iterable[str], details: Dict[str, Any]) -> Dict[str, Any]:
    markets = sorted({str(m).upper() for m in markets if str(m).upper() in ("KR", "US")})
    if set(markets) != {"KR", "US"}:
        raise BrokerError("preflight는 KR/US 모두 성공해야 합니다", code="live-preflight-incomplete")
    return _write_signed(PREFLIGHT_FILE, {
        "created_at": time.time(),
        "markets": markets,
        "details": details,
    })


def create_smoke_authorization(market: str, symbol: str, max_notional: float) -> Dict[str, Any]:
    """Create a one-time authorisation for an operator-approved live smoke trade."""
    market = str(market).upper()
    if market not in ("KR", "US") or not symbol or float(max_notional) <= 0:
        raise BrokerError("smoke test 승인 인자가 잘못되었습니다", code="live-smoke-authorization-invalid")
    return _write_signed(SMOKE_AUTH_FILE, {
        "created_at": time.time(),
        "expires_at": time.time() + 15 * 60,
        "market": market,
        "symbol": symbol.upper(),
        "max_notional": float(max_notional),
        "used": False,
    })


def consume_smoke_authorization(market: str, symbol: str, notional: float) -> Dict[str, Any]:
    record = _read_signed(SMOKE_AUTH_FILE)
    if record.get("used") or time.time() > float(record.get("expires_at", 0)):
        raise BrokerError("smoke test 승인이 만료되었거나 이미 사용되었습니다", code="live-smoke-authorization-expired")
    if (record.get("market"), record.get("symbol")) != (str(market).upper(), symbol.upper()):
        raise BrokerError("smoke test 주문이 승인된 시장/종목과 다릅니다", code="live-smoke-authorization-mismatch")
    if float(notional) > float(record.get("max_notional", 0)):
        raise BrokerError("smoke test 주문금액이 승인 한도를 초과했습니다", code="live-smoke-max-notional")
    record["used"] = True
    record["used_at"] = time.time()
    return _write_signed(SMOKE_AUTH_FILE, record)


def record_smoke(markets: Iterable[str], details: Dict[str, Any]) -> Dict[str, Any]:
    markets = sorted({str(m).upper() for m in markets if str(m).upper() in ("KR", "US")})
    return _write_signed(SMOKE_FILE, {
        "created_at": time.time(),
        "markets": markets,
        "details": details,
    })
