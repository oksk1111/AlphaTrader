"""
OPRO 스타일 리스크 파라미터 자가 최적화 (Optimization by PROmpting)

주간 백테스트 결과를 Gemini에 전달하여 리스크 파라미터 개선안을 제안받고
guardrail 이내에서 user_config.json에 반영합니다.

아이디어 출처:
  "Large Language Models as Optimizers" (OPRO, Yang et al. 2024)
  → LLM에게 현재 성과 지표 + 현재 파라미터를 보여주고,
    "더 높은 Sharpe / 낮은 MDD를 만들 파라미터를 제안하라" 고 반복 질의.
    guardrail 내 변경만 적용하여 리스크 최소화.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from modules.logger import logger

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "user_config.json"
BACKTEST_LATEST_FILE = BASE_DIR / "database" / "backtest_latest.json"
OPRO_HISTORY_FILE = BASE_DIR / "database" / "opro_history.json"

# 파라미터별 허용 범위
PARAM_BOUNDS: dict[str, dict] = {
    "stop_loss_pct":                {"min": -10.0, "max": -1.0,  "label": "Stop Loss (%)"},
    "trailing_stop_activation_pct": {"min":  1.0,  "max": 15.0,  "label": "Trailing Stop 활성화 (%)"},
    "trailing_stop_drop_pct":       {"min":  0.5,  "max":  5.0,  "label": "Trailing Stop 하락 (%)"},
    "gap_down_threshold_pct":       {"min":  1.0,  "max":  8.0,  "label": "갭다운 임계치 (%)"},
    "portfolio_drawdown_pct":       {"min":  3.0,  "max": 15.0,  "label": "포트폴리오 드로다운 (%)"},
}

# 한 번의 최적화에서 현재값 대비 최대 변동 비율 (30%)
MAX_CHANGE_RATIO = 0.30

# 설정 없을 때 사용하는 기본값 (run_bot.py 기본값과 동일)
DEFAULT_RISK_PARAMS: dict[str, float] = {
    "stop_loss_pct":                -3.0,
    "trailing_stop_activation_pct":  3.0,
    "trailing_stop_drop_pct":        1.5,
    "gap_down_threshold_pct":        3.0,
    "portfolio_drawdown_pct":        5.0,
}


# ─── 내부 유틸 ─────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[OPRO] JSON load error ({path}): {e}")
    return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _clamp_param(key: str, proposed: float, current: float) -> float:
    """
    guardrail 적용:
    1. 한 번에 현재값의 MAX_CHANGE_RATIO 이상 변경 불가
    2. PARAM_BOUNDS 절대 범위 제한
    """
    bounds = PARAM_BOUNDS.get(key)
    if bounds is None:
        return current

    # 최대 변동폭 제한
    max_delta = abs(current) * MAX_CHANGE_RATIO
    if abs(proposed - current) > max_delta:
        direction = 1 if proposed > current else -1
        proposed = current + direction * max_delta

    # 절대 범위 클램프
    proposed = max(bounds["min"], min(bounds["max"], proposed))
    return round(proposed, 2)


def _build_prompt(backtest: dict, current_params: dict, history: list) -> str:
    """OPRO 스타일 최적화 프롬프트 생성"""
    perf = backtest.get("performance", {})
    dist = backtest.get("distribution", {})
    period = backtest.get("period", {})

    def pct(v):
        return f"{v * 100:.2f}%" if v is not None else "N/A"

    params_lines = "\n".join(
        f"- {PARAM_BOUNDS[k]['label']}: {v}  (allowed range: {PARAM_BOUNDS[k]['min']} ~ {PARAM_BOUNDS[k]['max']})"
        for k, v in current_params.items()
        if k in PARAM_BOUNDS
    )

    history_block = ""
    if history:
        history_block = "\n## Previous Optimization History (recent 3 runs):\n"
        for h in history[-3:]:
            history_block += (
                f"- {h.get('date', '?')}: "
                f"params={h.get('suggested_params', {})} "
                f"→ Sharpe={h.get('score', {}).get('sharpe', '?')}, "
                f"MDD={h.get('score', {}).get('mdd', '?')}\n"
            )

    sharpe_val = perf.get('annualized_sharpe')
    sharpe_str = f"{sharpe_val:.2f}" if sharpe_val is not None else "N/A"

    return f"""You are a quantitative trading system optimizer using OPRO (Optimization by PROmpting).

## Objective
Maximize the Sharpe ratio and reduce Max Drawdown (MDD) by adjusting risk management parameters.

## Current Performance ({period.get('start', '?')} ~ {period.get('end', '?')}, {period.get('days', '?')} days)
- Cumulative Return: {pct(perf.get('cumulative_return'))}
- Max Drawdown (MDD): {pct(perf.get('max_drawdown'))}
- Annualized Sharpe Ratio: {sharpe_str}
- Daily Volatility: {pct(perf.get('daily_volatility'))}
- Win Rate: {pct(dist.get('win_rate'))}  (Win: {dist.get('win_days')}, Lose: {dist.get('lose_days')} days)

## Current Risk Parameters
{params_lines}
{history_block}
## Optimization Rules
- Each suggested value MUST stay within the specified allowed range.
- Do NOT change any single parameter by more than 30% of its current absolute value.
- If Sharpe > 1.5 and MDD < 10%: make only minor tweaks (within 10%).
- If MDD > 15%: tighten stop_loss_pct (closer to 0) and reduce trailing_stop_drop_pct.
- If Win Rate < 45%: consider slightly widening stop_loss_pct (more negative) to avoid premature exits.
- If Sharpe < 0.5: consider tightening trailing_stop_activation_pct to lock in gains earlier.

## Output
Reply with a single JSON object ONLY — no markdown, no explanation outside the JSON:
{{
  "stop_loss_pct": <number>,
  "trailing_stop_activation_pct": <number>,
  "trailing_stop_drop_pct": <number>,
  "gap_down_threshold_pct": <number>,
  "portfolio_drawdown_pct": <number>,
  "reasoning": "<one sentence explanation>"
}}"""


# ─── 공개 API ──────────────────────────────────────────────────────────────

def run_opro_optimization(notifier=None) -> dict:
    """
    백테스트 결과를 기반으로 Gemini에 파라미터 최적화를 요청하고
    guardrail 내에서 user_config.json에 반영합니다.

    Args:
        notifier: TelegramNotifier 인스턴스 (선택). 결과를 알림으로 발송.

    Returns:
        dict: {"changed": bool, "summary": str}
    """
    # 1. 백테스트 결과 로드
    backtest = _load_json(BACKTEST_LATEST_FILE, {})
    if backtest.get("status") != "ok":
        logger.info("[OPRO] 백테스트 데이터 부족. 최적화 건너뜀.")
        return {"changed": False, "summary": "백테스트 데이터 부족 — 최적화 건너뜀"}

    # 2. 현재 config 로드
    config = _load_json(CONFIG_FILE, {})
    current_risk = config.get("risk_management", {})
    current_params = {k: float(current_risk.get(k, DEFAULT_RISK_PARAMS[k])) for k in DEFAULT_RISK_PARAMS}

    # 3. 과거 최적화 이력 로드
    history = _load_json(OPRO_HISTORY_FILE, [])

    # 4. Gemini API 호출
    try:
        from config import GEMINI_API_KEY
    except ImportError:
        logger.warning("[OPRO] config 모듈에서 GEMINI_API_KEY를 가져올 수 없음.")
        return {"changed": False, "summary": "GEMINI_API_KEY 로드 실패"}

    if not GEMINI_API_KEY or "INSERT" in str(GEMINI_API_KEY):
        logger.warning("[OPRO] Gemini API Key 없음. 최적화 건너뜀.")
        return {"changed": False, "summary": "Gemini API Key 없음"}

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
        prompt = _build_prompt(backtest, current_params, history)
        response = model.generate_content(prompt, request_options={"timeout": 20})
        text = response.text.strip()
        # 마크다운 코드블럭 제거
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        suggested: dict = json.loads(text)
    except Exception as e:
        logger.error(f"[OPRO] Gemini 호출/파싱 실패: {e}")
        return {"changed": False, "summary": f"LLM 호출 실패: {e}"}

    # 5. guardrail 적용
    approved: dict[str, float] = {}
    for key in PARAM_BOUNDS:
        if key in suggested:
            try:
                raw = float(suggested[key])
                approved[key] = _clamp_param(key, raw, current_params[key])
            except (ValueError, TypeError):
                logger.warning(f"[OPRO] 파라미터 '{key}' 값 변환 실패: {suggested[key]}")

    reasoning = str(suggested.get("reasoning", ""))

    # 6. 유의미한 변경만 추려내기 (0.01 미만 변동 무시)
    meaningful: dict[str, float] = {
        k: v for k, v in approved.items()
        if abs(v - current_params.get(k, 0.0)) >= 0.01
    }

    if not meaningful:
        logger.info("[OPRO] 유의미한 파라미터 변경 없음. 현재 설정 유지.")
        return {"changed": False, "summary": "유의미한 변경 없음 — 현재 설정 최적"}

    # 7. config 업데이트
    config.setdefault("risk_management", {})
    config["risk_management"].update(meaningful)
    _save_json(CONFIG_FILE, config)
    logger.info(f"[OPRO] 파라미터 업데이트 완료: {meaningful}")

    # 8. 이력 저장 (최근 20회 유지)
    history.append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "previous_params": {k: current_params[k] for k in meaningful},
        "suggested_params": meaningful,
        "score": {
            "sharpe": backtest.get("performance", {}).get("annualized_sharpe"),
            "mdd": backtest.get("performance", {}).get("max_drawdown"),
            "cumulative_return": backtest.get("performance", {}).get("cumulative_return"),
        },
        "reasoning": reasoning,
    })
    _save_json(OPRO_HISTORY_FILE, history[-20:])

    # 9. 요약 & 알림
    change_lines = "\n".join(
        f"- {PARAM_BOUNDS[k]['label']}: {current_params[k]} → {v}"
        for k, v in meaningful.items()
    )
    perf = backtest.get("performance", {})
    sharpe_display = f"{perf.get('annualized_sharpe', 0):.2f}" if perf.get("annualized_sharpe") is not None else "N/A"
    mdd_display = f"{perf.get('max_drawdown', 0) * 100:.1f}%" if perf.get("max_drawdown") is not None else "N/A"

    summary = (
        f"🤖 [OPRO] 리스크 파라미터 자동 최적화 완료\n\n"
        f"📊 백테스트 성과: Sharpe={sharpe_display}, MDD={mdd_display}\n\n"
        f"🔧 변경사항:\n{change_lines}\n\n"
        f"💡 이유: {reasoning}"
    )

    if notifier:
        try:
            notifier.send_message(summary)
        except Exception as _ne:
            logger.warning(f"[OPRO] Telegram 알림 실패: {_ne}")

    logger.info(f"[OPRO] 최적화 완료.\n{summary}")
    return {"changed": True, "summary": summary}
