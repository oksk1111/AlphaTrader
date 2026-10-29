"""Live-order release gate tests."""

import json
import time

import pytest

from modules.brokers.models import BrokerError, Market, OrderRequest, OrderSide, OrderType
from modules.brokers.toss import TossBroker
from modules import live_gate


class Response:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._body


class Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response({"result": {}})


def _configure(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_GATE_SECRET", "gate-secret-at-least-32-bytes-long")
    monkeypatch.setattr(live_gate, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live_gate, "PREFLIGHT_FILE", tmp_path / "preflight.json")
    monkeypatch.setattr(live_gate, "SMOKE_FILE", tmp_path / "smoke.json")
    monkeypatch.setattr(live_gate, "SMOKE_AUTH_FILE", tmp_path / "smoke-auth.json")
    monkeypatch.setattr(live_gate, "HEALTH_FILE", tmp_path / "health.json")


def _record_shadow_history(path, count=20):
    sessions = []
    for market in ("KR", "US"):
        for number in range(count):
            sessions.append({
                "session_key": f"{market}:2026-10-{number + 1:02d}",
                "market": market,
                "buys": 0,
                "candidates": 1,
                "notes": "strategy=momentum_rotation execution_mode=shadow preflight=ok",
            })
    path.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")


def test_live_order_is_blocked_without_deploy_preflight(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    session = Session()
    broker = TossBroker("client", "secret", "1", "live", session)
    broker.access_token, broker.token_expiry = "token", 10 ** 12
    with pytest.raises(BrokerError) as caught:
        broker.submit_order(OrderRequest("069500", Market.KR, OrderSide.BUY, OrderType.LIMIT, 1, price=70000))
    assert caught.value.code == "live-preflight-missing"
    assert session.calls == []


def test_live_orders_need_no_operator_approval(monkeypatch, tmp_path):
    """Autonomous trading: a deploy-time preflight is enough — no shadow count,
    smoke test, release record or commit binding."""
    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv("DEPLOYED_COMMIT", raising=False)
    live_gate.record_preflight(("KR", "US"), {"quotes": {}})
    live_gate.assert_live_order_allowed(Market.KR)
    live_gate.assert_live_order_allowed(Market.US)


def test_old_preflight_still_allows_live_orders(monkeypatch, tmp_path):
    """Preflight runs once per deploy; sessions on later days must keep trading."""
    _configure(monkeypatch, tmp_path)
    live_gate.record_preflight(("KR", "US"), {"quotes": {}})
    payload = live_gate._read_signed(live_gate.PREFLIGHT_FILE)
    payload["created_at"] = time.time() - 30 * 24 * 60 * 60
    live_gate._write_signed(live_gate.PREFLIGHT_FILE, payload)
    live_gate.assert_live_order_allowed(Market.US)


def test_tampered_preflight_blocks_live_orders(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    live_gate.record_preflight(("KR", "US"), {"quotes": {}})
    body = json.loads(live_gate.PREFLIGHT_FILE.read_text(encoding="utf-8"))
    body["markets"] = ["KR", "US", "XX"]
    live_gate.PREFLIGHT_FILE.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(BrokerError) as caught:
        live_gate.assert_live_order_allowed(Market.KR)
    assert caught.value.code == "live-preflight-missing"


@pytest.mark.parametrize("how", ["file", "env"])
def test_emergency_stop_blocks_live_orders(monkeypatch, tmp_path, how):
    _configure(monkeypatch, tmp_path)
    live_gate.record_preflight(("KR", "US"), {"quotes": {}})
    if how == "file":
        (tmp_path / "live_halt").write_text("manual stop", encoding="utf-8")
    else:
        monkeypatch.setenv("LIVE_TRADING_HALT", "1")
    with pytest.raises(BrokerError) as caught:
        live_gate.assert_live_order_allowed(Market.KR)
    assert caught.value.code == "live-trading-halted"
    status = live_gate.gate_status()
    assert status["halted"] is True
    assert status["markets"]["KR"]["live_allowed"] is False


def test_gate_status_reports_live_allowed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    _record_shadow_history(live_gate.HEALTH_FILE, count=3)
    assert live_gate.gate_status()["markets"]["KR"]["live_allowed"] is False
    live_gate.record_preflight(("KR", "US"), {"quotes": {}})

    status = live_gate.gate_status()

    assert status["shadow_sessions"] == {"KR": 3, "US": 3}
    assert status["markets"]["KR"]["live_allowed"] is True
    assert status["markets"]["US"]["preflight_ok"] is True
    assert status["halted"] is False


def test_one_time_smoke_authorization_enforces_symbol_and_cap(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    live_gate.create_smoke_authorization("US", "XLK", 250)
    with pytest.raises(BrokerError) as caught:
        live_gate.consume_smoke_authorization("US", "XLK", 251)
    assert caught.value.code == "live-smoke-max-notional"
    live_gate.consume_smoke_authorization("US", "XLK", 249)
    with pytest.raises(BrokerError) as caught:
        live_gate.consume_smoke_authorization("US", "XLK", 249)
    assert caught.value.code == "live-smoke-authorization-expired"
