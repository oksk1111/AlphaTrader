"""Live-order release gates.

`EXECUTION_MODE=live` means the broker is capable of transmitting an order; it is
not itself permission to do so.  Every live order is blocked unless this module
can verify a short-lived, HMAC-signed release record created after read-only
preflight, shadow evidence, and the operator-approved smoke test.
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
RELEASE_FILE = STATE_DIR / "live_release.json"
SMOKE_AUTH_FILE = STATE_DIR / "live_smoke_authorization.json"
HEALTH_FILE = STATE_DIR / "strategy_health.json"

PREFLIGHT_MAX_AGE_SEC = 60 * 60
SMOKE_MAX_AGE_SEC = 7 * 24 * 60 * 60
RELEASE_MAX_AGE_SEC = 24 * 60 * 60
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


def readiness(required: int = DEFAULT_REQUIRED_SHADOW_SESSIONS) -> Dict[str, Any]:
    evidence = shadow_evidence(required)
    result: Dict[str, Any] = {
        "required_shadow_sessions": max(1, int(required)),
        "shadow_sessions": evidence,
        "markets": {},
    }
    preflight = _read_signed(PREFLIGHT_FILE)
    _fresh(preflight, PREFLIGHT_MAX_AGE_SEC, "live-preflight-stale", "read-only preflight")
    smoke = _read_signed(SMOKE_FILE)
    _fresh(smoke, SMOKE_MAX_AGE_SEC, "live-smoke-stale", "live smoke test")
    for market in ("KR", "US"):
        result["markets"][market] = {
            "shadow_ok": evidence[market] >= result["required_shadow_sessions"],
            "preflight_ok": market in (preflight.get("markets") or []),
            "smoke_ok": market in (smoke.get("markets") or []),
        }
    return result


def authorize_release(required: int = DEFAULT_REQUIRED_SHADOW_SESSIONS, commit: str = "") -> Dict[str, Any]:
    """Create the short-lived normal-order release after all non-order gates pass."""
    report = readiness(required)
    markets = [market for market, checks in report["markets"].items() if all(checks.values())]
    if not markets:
        raise BrokerError("live 전환 조건 미충족: shadow 증적·preflight·smoke test를 확인하세요", code="live-gate-not-ready")
    return _write_signed(RELEASE_FILE, {
        "created_at": time.time(),
        "expires_at": time.time() + RELEASE_MAX_AGE_SEC,
        "markets": markets,
        "required_shadow_sessions": report["required_shadow_sessions"],
        "commit": commit,
    })


def assert_live_order_allowed(market: Market) -> None:
    """Hard broker-side guard for every non-smoke live order."""
    deployed_commit = os.getenv("DEPLOYED_COMMIT", "").strip()
    if not deployed_commit:
        raise BrokerError("DEPLOYED_COMMIT이 없어 live 주문을 허용할 수 없습니다", code="live-deploy-commit-missing")
    release = _read_signed(RELEASE_FILE)
    if release.get("commit") != deployed_commit:
        raise BrokerError("현재 배포본에 대한 live release가 아닙니다", code="live-release-commit-mismatch")
    if time.time() > float(release.get("expires_at", 0)):
        raise BrokerError("live release가 만료되었습니다. 새 preflight와 배포 승인이 필요합니다", code="live-release-expired")
    if market.value not in (release.get("markets") or []):
        raise BrokerError(f"{market.value} 시장은 live 승인되지 않았습니다", code="live-market-not-approved")


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
