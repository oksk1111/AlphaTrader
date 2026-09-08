"""
health_monitor.py — v6.0 전략 자가진단 루프 (Loop Engineering)

[왜 이 모듈이 생겼는가]

이 프로젝트의 진짜 문제는 "전략이 틀렸다"가 아니라 **"틀린 걸 몇 달 동안 아무도
몰랐다"** 는 것이다. 봇은 매일 정상적으로 부팅했고, 하트비트를 찍었고, 세션을 열었고,
게이트를 평가했고, 전부 차단한 뒤 조용히 종료했다. 어떤 단계에서도 에러가 나지 않았다.
**정상 동작하는 것처럼 보이면서 아무 일도 하지 않는 것**이 가장 나쁜 실패 모드다.

v5.0 이 "매수 0건 워치독"을 넣긴 했지만 조건이 `session_buy_count == 0 and tickers
and not holding` 이었다 — 보유 종목이 하나라도 있으면 침묵한다. 그리고 단발성이라
"오늘 0건"은 알려도 "40세션 연속 0건"은 알리지 못했다. 하루 0건은 정상이지만
40일 연속 0건은 고장이다. 이 둘을 구분하지 못하면 알림은 노이즈가 되어 무시된다.

[v6.0 루프]

  기록(record_session) → 누적 판정(diagnose) → 등급별 에스컬레이션 → 조치 힌트

  - 세션마다 매수/매도 건수, 후보 수, 차단 사유 분포, 현금, 낙폭을 남긴다.
  - 연속 무매수 세션 수를 추적한다. 3세션이면 WARN, 7세션이면 CRITICAL.
  - **지배적 차단 사유**를 집계해 "왜" 를 같이 알린다. 단순히 "거래가 없습니다"가
    아니라 "12세션 연속 무매수, 원인의 92%가 '주문가능금액 부족' → USD 예수금/
    통합증거금 설정을 확인하세요" 라고 말해야 사람이 움직인다.
  - 진단 결과는 디스크에 남아 대시보드에서도 조회 가능하다.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
HEALTH_FILE = os.path.join(STATE_DIR, "strategy_health.json")

MAX_SESSIONS_KEPT = 120
WARN_DRY_SESSIONS = 3       # 연속 무매수 3세션 → 경고
CRITICAL_DRY_SESSIONS = 7   # 연속 무매수 7세션 → 심각 (사람 개입 필요)

# 지배적 차단 사유 → 사람이 실제로 취할 수 있는 조치
REMEDY_HINTS = {
    "주문가능금액": (
        "USD 예수금이 0일 가능성이 큽니다. KIS 계좌의 외화 예수금 또는 "
        "통합증거금(원화주문) 설정을 확인하세요. get_foreign_balance() 응답도 함께 점검."
    ),
    "노출 한도": "종목별 노출 상한에 도달했습니다. max_position_pct 를 올리거나 일부 익절하세요.",
    "낙폭 서킷브레이커": "계좌 낙폭이 커서 매수가 중단됐습니다. risk_state.json 확인 후 회복을 기다리거나 수동 리셋하세요.",
    "CRASH": "AI 가 시장/섹터 붕괴로 판정 중입니다. 지속되면 llm_consensus.crash_veto 또는 뉴스 소스를 점검하세요.",
    "상관그룹": "상관그룹 한도로 신규 진입이 막혔습니다. correlation_max_per_group 을 조정하세요.",
    "낙하칼날": "전 이동평균 하회 + 반등 미확인 상태가 지속 중입니다. 하락장이라면 정상 동작입니다.",
    "손실 한도": "일일 손실 한도에 걸렸습니다. losing_streak 설정을 확인하세요.",
    "폐장": "세션 진입이 폐장 직전에만 이뤄지고 있습니다. 트리거 시각/스케줄러를 점검하세요.",
    "현재가 조회 실패": "시세 조회가 실패하고 있습니다. KIS API 토큰/거래소 코드(exchange)를 점검하세요.",
    "점수": "판단 점수가 계속 기준 미달입니다. 시장이 약세이거나 min_buy_score 가 너무 높습니다.",
}


@dataclass
class SessionRecord:
    session_key: str
    market: str
    started_at: str
    ended_at: str = ""
    candidates: int = 0
    buys: int = 0
    sells: int = 0
    evaluations: int = 0
    available_cash: float = 0.0
    drawdown_pct: float = 0.0
    block_reasons: Dict[str, int] = field(default_factory=dict)
    notes: str = ""


@dataclass
class Diagnosis:
    level: str                  # 'ok' | 'info' | 'warn' | 'critical'
    dry_streak: int
    message: str
    dominant_reason: str = ""
    dominant_share: float = 0.0
    remedy: str = ""

    @property
    def should_alert(self) -> bool:
        return self.level in ("warn", "critical")


def _load() -> Dict[str, Any]:
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sessions": []}


def _save(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = HEALTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, HEALTH_FILE)
    except Exception as e:
        logger.error("[Health] 저장 실패: %s", e)


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def record_session(record: SessionRecord) -> None:
    """세션 종료 시 1회 호출. 같은 session_key 는 덮어쓴다(재시도 대응)."""
    data = _load()
    sessions: List[Dict[str, Any]] = [
        s for s in data.get("sessions", []) if s.get("session_key") != record.session_key
    ]
    rec = record.__dict__.copy()
    rec["ended_at"] = rec.get("ended_at") or _now()
    sessions.append(rec)
    sessions = sessions[-MAX_SESSIONS_KEPT:]
    data["sessions"] = sessions
    data["updated_at"] = _now()
    _save(data)


def _recent(market: Optional[str], limit: int) -> List[Dict[str, Any]]:
    sessions = _load().get("sessions", [])
    if market:
        sessions = [s for s in sessions if s.get("market") == market]
    return sessions[-limit:]


def dry_streak(market: Optional[str] = None) -> int:
    """가장 최근부터 거슬러 올라가며 '후보는 있었는데 매수 0건'인 연속 세션 수."""
    streak = 0
    for s in reversed(_recent(market, MAX_SESSIONS_KEPT)):
        if int(s.get("buys", 0) or 0) > 0:
            break
        if int(s.get("candidates", 0) or 0) <= 0:
            # 후보 자체가 없었던 세션은 '무매수'로 세지 않는다(휴장/포트폴리오 공백).
            continue
        streak += 1
    return streak


def diagnose(market: Optional[str] = None) -> Diagnosis:
    """누적 기록으로 전략이 실제로 '동작 중'인지 판정한다."""
    streak = dry_streak(market)
    scope = market or "전체"

    if streak == 0:
        return Diagnosis(level="ok", dry_streak=0,
                         message=f"[{scope}] 정상 — 최근 세션에서 체결 발생")

    # 무매수 구간의 차단 사유 집계
    counter: Counter = Counter()
    for s in reversed(_recent(market, MAX_SESSIONS_KEPT)):
        if int(s.get("buys", 0) or 0) > 0:
            break
        for reason, n in (s.get("block_reasons") or {}).items():
            counter[reason] += int(n or 0)

    dominant, share, remedy = "", 0.0, ""
    if counter:
        dominant, top_n = counter.most_common(1)[0]
        total = sum(counter.values()) or 1
        share = round(top_n / total * 100.0, 1)
        for key, hint in REMEDY_HINTS.items():
            if key in dominant:
                remedy = hint
                break

    if streak >= CRITICAL_DRY_SESSIONS:
        level = "critical"
        head = (f"🚨 [{scope}] {streak}세션 연속 매수 0건 — 전략이 사실상 정지 상태입니다. "
                f"사람의 확인이 필요합니다.")
    elif streak >= WARN_DRY_SESSIONS:
        level = "warn"
        head = f"⚠️ [{scope}] {streak}세션 연속 매수 0건 — 차단 사유를 확인하세요."
    else:
        level = "info"
        head = f"ℹ️ [{scope}] {streak}세션 연속 매수 0건 (아직 정상 범위)"

    detail = ""
    if dominant:
        detail = f"\n· 지배적 차단 사유: {dominant} ({share}%)"
    if remedy:
        detail += f"\n· 조치: {remedy}"

    return Diagnosis(level=level, dry_streak=streak, message=head + detail,
                     dominant_reason=dominant, dominant_share=share, remedy=remedy)


def summary(market: Optional[str] = None, limit: int = 10) -> str:
    """대시보드/로그용 최근 세션 요약."""
    rows = _recent(market, limit)
    if not rows:
        return "기록된 세션이 없습니다."
    lines = ["세션            시장  후보  매수  매도  현금        낙폭"]
    for s in rows:
        lines.append(
            f"{str(s.get('session_key','?')):<15} {str(s.get('market','?')):<4} "
            f"{int(s.get('candidates',0)):>4} {int(s.get('buys',0)):>5} "
            f"{int(s.get('sells',0)):>5}  {float(s.get('available_cash',0)):>10,.0f} "
            f"{float(s.get('drawdown_pct',0)):>5.1f}%"
        )
    return "\n".join(lines)
