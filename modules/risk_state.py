"""
risk_state.py — v6.0 지속형 리스크 상태 (진짜 서킷브레이커)

[왜 이 모듈이 생겼는가]
v5.0/v5.1 의 "포트폴리오 드로다운 서킷브레이커"는 서킷브레이커가 아니라 **영구 래치**
였다. `strategies/technical.check_portfolio_drawdown()` 이 계산하는 값은

    (매입원가 - 평가금액) / 매입원가

즉 '고점 대비 낙폭(drawdown)' 이 아니라 **원가 대비 현재 평가손실**이다. 임계치를
7% 로 두면, 계좌가 원가 대비 7% 물리는 순간 `portfolio_drawdown_halt=True` 가 되어
신규 매수가 전면 차단되는데 — **해제 조건이 코드 어디에도 없다.**

그리고 이건 자기강화적이다:
    매수 차단 → 원가 변동 없음 → 여전히 -7% → 다음 세션도 차단 → …
계좌가 스스로 반등하지 않는 한 봇은 **영원히 아무것도 사지 않는다.**
"몇 달째 거래가 안 된다"의 직접적 원인 중 하나.

v6.0:
  - 고점(peak equity) 대비 낙폭으로 계산한다. 진짜 drawdown.
  - HALT 는 **시간 제한(cool-off)** 이 있다. 기본 3영업일 후 자동으로 축소 모드로
    복귀하고, 낙폭이 절반으로 회복되면 즉시 해제한다.
  - HALT 중에도 완전 차단이 아니라 **사이즈 축소(de-risk) 모드**로 내려간다.
    "아무것도 안 함"은 리스크 관리가 아니라 전략의 죽음이다.
  - 모든 상태 전이를 디스크에 남긴다 → 프로세스 재시작에도 살아남고, 왜 막혔는지
    사후 추적이 가능하다.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
STATE_FILE = os.path.join(STATE_DIR, "risk_state.json")

# 낙폭 단계 → 사이즈 배율. 완전 차단(0.0)은 '진짜 붕괴' 구간에서만.
DD_LADDER = [
    (0.0, 1.0),    # 낙폭 0~7%   : 정상
    (7.0, 0.6),    # 낙폭 7~12%  : 60% 사이즈
    (12.0, 0.3),   # 낙폭 12~18% : 30% 사이즈 (방어적 축적)
    (18.0, 0.0),   # 낙폭 18%+   : 신규 매수 중단 (cool-off 적용)
]
HARD_HALT_DD_PCT = 18.0
COOLOFF_DAYS = 3
RECOVERY_RELEASE_RATIO = 0.5   # 낙폭이 HALT 시점의 절반으로 줄면 즉시 해제


@dataclass
class RiskSnapshot:
    peak_equity: float = 0.0
    last_equity: float = 0.0
    drawdown_pct: float = 0.0
    size_multiplier: float = 1.0
    halted: bool = False
    halt_started: Optional[str] = None
    halt_dd_pct: float = 0.0
    reason: str = ""
    updated_at: str = ""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _load() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error("[RiskState] 저장 실패: %s", e)


def _ladder_multiplier(dd_pct: float) -> float:
    mult = 1.0
    for threshold, m in DD_LADDER:
        if dd_pct >= threshold:
            mult = m
    return mult


def update_equity(total_equity: float, market: str = "") -> RiskSnapshot:
    """세션 시작 시 총자산을 넣어 호출한다. 고점 갱신 + 낙폭 + 사이즈 배율을 반환.

    total_equity: 예수금 + 평가금액 (동일 통화 기준으로 일관되게 넣을 것)
    """
    data = _load()
    try:
        total_equity = float(total_equity or 0.0)
    except (TypeError, ValueError):
        total_equity = 0.0

    if total_equity <= 0:
        # 잔고 조회 실패로 0 이 들어오면 고점을 오염시키면 안 된다 — 이전 상태 유지.
        logger.warning("[RiskState] total_equity<=0 (%s) — 상태 갱신 건너뜀", total_equity)
        return snapshot()

    peak = max(float(data.get("peak_equity", 0.0) or 0.0), total_equity)
    dd_pct = ((peak - total_equity) / peak * 100.0) if peak > 0 else 0.0
    dd_pct = round(max(dd_pct, 0.0), 2)

    halted = bool(data.get("halted", False))
    halt_started = data.get("halt_started")
    halt_dd = float(data.get("halt_dd_pct", 0.0) or 0.0)
    reason = ""

    if halted:
        released = False
        if dd_pct <= halt_dd * RECOVERY_RELEASE_RATIO:
            released, reason = True, f"낙폭 회복 ({halt_dd:.1f}% → {dd_pct:.1f}%) — HALT 해제"
        elif halt_started:
            try:
                started = _dt.datetime.fromisoformat(halt_started)
                if (_dt.datetime.now(started.tzinfo) - started).days >= COOLOFF_DAYS:
                    released, reason = True, f"cool-off {COOLOFF_DAYS}일 경과 — 축소 모드로 복귀"
            except Exception:
                released, reason = True, "halt_started 파싱 실패 — 안전하게 해제"
        if released:
            halted, halt_started, halt_dd = False, None, 0.0
            logger.warning("[RiskState] %s", reason)

    if not halted and dd_pct >= HARD_HALT_DD_PCT:
        halted, halt_started, halt_dd = True, _now_iso(), dd_pct
        reason = f"낙폭 {dd_pct:.1f}% ≥ {HARD_HALT_DD_PCT}% — 신규 매수 중단 (최대 {COOLOFF_DAYS}영업일)"
        logger.critical("[RiskState] %s", reason)

    multiplier = 0.0 if halted else _ladder_multiplier(dd_pct)

    snap = RiskSnapshot(
        peak_equity=round(peak, 2),
        last_equity=round(total_equity, 2),
        drawdown_pct=dd_pct,
        size_multiplier=multiplier,
        halted=halted,
        halt_started=halt_started,
        halt_dd_pct=halt_dd,
        reason=reason or f"낙폭 {dd_pct:.1f}% → 사이즈 {multiplier:.0%}",
        updated_at=_now_iso(),
    )

    history: List[Dict[str, Any]] = list(data.get("history", []))[-199:]
    history.append({"at": snap.updated_at, "market": market,
                    "equity": snap.last_equity, "dd": dd_pct, "mult": multiplier})
    out = asdict(snap)
    out["history"] = history
    _save(out)
    return snap


def snapshot() -> RiskSnapshot:
    """현재 저장된 리스크 상태 (갱신 없이 읽기만)."""
    data = _load()
    return RiskSnapshot(
        peak_equity=float(data.get("peak_equity", 0.0) or 0.0),
        last_equity=float(data.get("last_equity", 0.0) or 0.0),
        drawdown_pct=float(data.get("drawdown_pct", 0.0) or 0.0),
        size_multiplier=float(data.get("size_multiplier", 1.0) or 0.0),
        halted=bool(data.get("halted", False)),
        halt_started=data.get("halt_started"),
        halt_dd_pct=float(data.get("halt_dd_pct", 0.0) or 0.0),
        reason=data.get("reason", ""),
        updated_at=data.get("updated_at", ""),
    )


def reset(keep_peak: bool = True) -> None:
    """수동 리셋 (대시보드/운영용)."""
    data = _load()
    peak = data.get("peak_equity", 0.0) if keep_peak else 0.0
    _save({"peak_equity": peak, "halted": False, "halt_started": None,
           "halt_dd_pct": 0.0, "reason": "manual reset", "updated_at": _now_iso(),
           "history": data.get("history", [])})
    logger.warning("[RiskState] 수동 리셋 완료 (peak 유지=%s)", keep_peak)
