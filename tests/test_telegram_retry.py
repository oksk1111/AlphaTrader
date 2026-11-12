"""[v8.5] 텔레그램 일시 실패 재시도 — 2026-11-03 15:00 장 마감 요약이 한 번의 실패로 사라졌다."""
from modules import telegram_notifier as tn


class R:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)

    def json(self):
        return self._body


def _notifier(monkeypatch, outcomes):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(dict(json))
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(tn.requests, "post", post)
    monkeypatch.setattr(tn.time, "sleep", lambda s: None)
    return tn.TelegramNotifier("token", "chat"), calls


def test_network_error_is_retried_once(monkeypatch):
    n, calls = _notifier(monkeypatch, [ConnectionError("reset"), R(200)])
    assert n.send_message("hi") is True and len(calls) == 2


def test_rate_limit_and_server_error_are_retried_once(monkeypatch):
    n, calls = _notifier(monkeypatch, [R(429, {"parameters": {"retry_after": 3}}), R(200)])
    assert n.send_message("hi") is True and len(calls) == 2
    n, calls = _notifier(monkeypatch, [R(502), R(502)])
    assert n.send_message("hi") is False and len(calls) == 2, "두 번까지만 — 세션을 붙잡지 않는다"


def test_bad_markup_still_falls_back_to_plain_text(monkeypatch):
    n, calls = _notifier(monkeypatch, [R(400, {"description": "can't parse entities"}), R(200)])
    assert n.send_message("<b>broken") is True
    assert calls[0]["parse_mode"] == "HTML" and calls[1]["parse_mode"] == ""
