"""[v8.2] 지난 세션의 판단 로그를 원격에서 읽을 수 있는가.

2026-10-30 KR 세션이 거래 0건으로 끝났는데 대시보드는 현재 로그 파일의 마지막
160줄(1분 heartbeat)만 보여줘서, 그것이 판단(RISK-OFF)인지 고장(주문 거부)인지
SSH 없이는 알 수 없었다. 토스 주문 거부 상세는 print 로 stdout 에만 남는다.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "dashboard-key")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    import web.auth as auth
    import web.app as app
    importlib.reload(auth)
    return importlib.reload(app)


def test_excerpt_reads_rotated_log_and_stdout_for_one_session(monkeypatch, tmp_path):
    app = _app(monkeypatch)
    db = tmp_path / "database"
    db.mkdir()
    rotated = db / "trading.log.2026-10-30"
    rotated.write_text(
        "2026-10-29 15:00:00,000 - INFO - [bot] [KR] 어제 줄\n"
        "2026-10-30 09:10:01,000 - INFO - [bot] Heartbeat: Bot is alive... Market Status: KR\n"
        "2026-10-30 09:10:02,000 - INFO - [bot] [KR] 로테이션 판단 (리밸런싱일): RISK-OFF(현금) slots=2\n"
        "2026-10-30 09:10:03,000 - INFO - [bot] [KR] 방어자산 슬롯: ['148070']\n"
        "2026-10-30 22:40:01,000 - INFO - [bot] [US] 저녁 줄\n",
        encoding="utf-8")
    (db / "bot_stdout.log").write_text(
        "2026-10-30 09:10:03,000 - INFO - [bot] [KR] 방어자산 슬롯: ['148070']\n"   # 중복 → 한 번만
        "[TOSS-KR] 주문 거부 BUY 148070 x6: 테스트 (code-x, requestId=r1)\n",      # 타임스탬프 없음
        encoding="utf-8")
    monkeypatch.setattr(app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(app, "bot_log_files", lambda limit_files=40: [str(rotated)])

    rows = app.session_log_excerpt("2026-10-30", "08:50", "15:40")
    text = [r["line"] for r in rows]

    assert any("RISK-OFF" in t for t in text)
    assert sum("방어자산 슬롯" in t for t in text) == 1          # trading.log·stdout 중복 제거
    reject = [r for r in rows if "주문 거부" in r["line"]]
    assert reject and reject[0]["time"] == "09:10:03"           # 앞 줄의 시각을 물려받는다
    assert not any("Heartbeat" in t for t in text)               # 1분 잡음 제외
    assert not any("어제" in t or "저녁" in t for t in text)      # 날짜·시간 구간 밖 제외


def test_session_diagnostics_requires_auth(monkeypatch):
    app = _app(monkeypatch)
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    assert client.get("/api/session-diagnostics?date=2026-10-30").status_code == 401
    ok = client.get("/api/session-diagnostics?date=2026-10-30&key=dashboard-key")
    assert ok.status_code == 200
    body = ok.json()
    assert body["date"] == "2026-10-30" and "rotation_state" in body and "lines" in body
