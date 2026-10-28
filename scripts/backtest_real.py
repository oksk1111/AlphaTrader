"""
scripts/backtest_real.py — v7.0 실데이터 포트폴리오 백테스트

[왜 필요한가]

verify_strategy_loop.py 는 합성 GBM 가격 + 단일 종목이다. 그래서 두 가지를
원리적으로 볼 수 없었다:

  1. **무엇을 들고 있느냐** — 21종목에 나눠 사는 것과 강한 2종목에 모는 것의 차이는
     단일 종목 시뮬에서는 존재하지 않는다.
  2. **실제 시장의 추세 지속성** — GBM 은 모멘텀이 0 이다(정의상 무작위 보행).
     모멘텀 전략은 GBM 에서 절대 이길 수 없고, 실제 시장에서만 검증 가능하다.

이 스크립트는 실제 일봉(수정주가)으로 여러 종목 포트폴리오를 돌린다.
데이터: 토스증권 캔들 API (TOSS_CLIENT_ID 가 있고 허용 IP 에서 실행할 때, 일봉은 기간 제한 없음).
        그 외 환경(로컬 연구 PC 등)에서는 yfinance 로 받는다. 둘 다 data_cache/ 에 캐시.

  · 현행(v6.1 근사) : 전 후보에 대해 매일 decision_engine.evaluate_buy() → DCA 사이징
                      (run_bot.calculate_dca_quantity 와 같은 규칙) + 일중 고/저가로
                      손절 -12% / 트레일링 +10%·6% / 추세붕괴 청산
  · 로테이션         : modules/momentum_rotation.plan_rotation() — 봇과 같은 함수

신호는 t 일 종가로 계산하고 **t+1 일 시가에 체결**한다 (룩어헤드 없음).
비용: KR 수수료 0.015%+슬리피지 0.1%(개별주 매도세 0.18%), US 수수료 0.25%+슬리피지 0.05%.

실행:
    python scripts/backtest_real.py                 # KR + US, 전체 변형
    python scripts/backtest_real.py --market KR --start 2016-01-01
    python scripts/backtest_real.py --json out.json # 결과 저장
    python scripts/backtest_real.py --robustness    # [v8.0] 운영설정 과적합 점검:
                                                    #   train 2017-21 / test 2022- / 시작연도 6개

⚠️ 생존편향: 유니버스는 '지금 봇이 들고 있는 후보 목록'이다. 과거에 상장폐지된
   ETF 는 없다. 절대 수익률보다 **같은 유니버스 안에서의 상대 비교**를 보라.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from modules.decision_engine import TickerContext, PortfolioContext, evaluate_buy  # noqa: E402
from modules.momentum_rotation import (  # noqa: E402
    compute_features, market_risk_on, plan_rotation, fillable_slots, defensive_targets,
)

CACHE_DIR = os.path.join(ROOT, "data_cache")

# ---------------------------------------------------------------------------
# 유니버스 — 2026-10-01 시점 봇이 실제로 순회하던 후보 (run_bot.py / portfolio_manager.py)
# yfinance 가 영문자 포함 신규 코드(0174B0 등)를 지원하지 않아 조회 가능한 것만 쓴다.
# ---------------------------------------------------------------------------
KR_ETFS = {
    "069500": "KODEX 200", "091160": "KODEX 반도체", "305720": "KODEX 2차전지산업",
    "364980": "TIGER 2차전지K-뉴딜", "292150": "TIGER 코리아TOP10",
    "133690": "TIGER 미국나스닥100", "379800": "KODEX 미국S&P500",
    "381180": "TIGER 미국필라델피아반도체", "426030": "TIME 미국나스닥100액티브",
    "456600": "TIME 글로벌AI인공지능", "487230": "KODEX 미국AI전력핵심인프라",
    "495230": "KoAct 코리아밸류업",
}
KR_STOCKS = {  # 현행 run_bot.TARGET_TICKERS_KR_1X 에 하드코딩돼 있던 개별주
    "000660": "SK하이닉스", "005930": "삼성전자", "012450": "한화에어로스페이스",
    "005380": "현대차", "035420": "NAVER",
}
KR_BENCH = "069500"

US_1X = {"SPY": "S&P500", "QQQ": "Nasdaq100", "SMH": "Semis",
         "IWM": "Russell2000", "XLK": "Technology"}
US_3X = {"TQQQ": "Nasdaq 3x", "SOXL": "Semis 3x"}
US_BENCH = "SPY"


def _operational_universe(market: str) -> List[str]:
    """운영 설정의 유니버스를 읽는다 — 검증과 실제 대상이 달라지지 않게 한다."""
    with open(os.path.join(ROOT, "user_config.json"), encoding="utf-8") as f:
        config = json.load(f)
    raw = ((config.get("momentum_rotation") or {}).get(market.lower()) or {}).get("universe") or []
    symbols = []
    for item in raw:
        symbol = (item.get("symbol") or item.get("code")) if isinstance(item, dict) else str(item)
        if symbol:
            symbols.append(symbol)
    return symbols


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
def yf_symbol(code: str, market: str) -> str:
    return f"{code}.KS" if market == "KR" else code


def _download_toss(code: str, market: str) -> Optional[pd.DataFrame]:
    from modules.brokers.compat import TossDomestic, TossOverseas
    rows = (TossOverseas() if market == "US" else TossDomestic()).get_daily_history(
        code, bars=3500, max_pages=20)
    if not rows:
        return None
    df = pd.DataFrame(rows).rename(columns=str.capitalize)
    df.index = pd.to_datetime(df.pop("Date"), format="%Y%m%d")
    return df[["Open", "High", "Low", "Close"]]


def _download_yf(code: str, market: str) -> Optional[pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(yf_symbol(code, market), start="2014-01-01", progress=False,
                      auto_adjust=True, threads=False)
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return raw[["Open", "High", "Low", "Close"]].dropna()


def load_prices(codes: List[str], market: str, start: str, refresh: bool = False,
                source: str = "auto") -> Dict[str, pd.DataFrame]:
    if source == "auto":
        source = "toss" if os.getenv("TOSS_CLIENT_ID") else "yfinance"
    fetch = _download_toss if source == "toss" else _download_yf
    os.makedirs(CACHE_DIR, exist_ok=True)
    out: Dict[str, pd.DataFrame] = {}
    for code in codes:
        path = os.path.join(CACHE_DIR, f"{market}_{code}.csv")
        df = None
        if os.path.exists(path) and not refresh:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            try:
                df = fetch(code, market)
            except Exception as e:  # 네트워크/심볼 오류는 해당 종목만 제외
                print(f"  [skip] {code}: {e}")
                continue
            if df is None or df.empty:
                print(f"  [skip] {code}: no data")
                continue
            df.to_csv(path)
        df = df[df["Close"] > 0]
        out[code] = df
    return out


# ---------------------------------------------------------------------------
# 포트폴리오 회계
# ---------------------------------------------------------------------------
@dataclass
class Cost:
    buy: float
    sell: float
    sell_tax_stock: float = 0.0


COSTS = {"KR": Cost(buy=0.00115, sell=0.00115, sell_tax_stock=0.0018),
         "US": Cost(buy=0.0030, sell=0.0030)}


@dataclass
class Pos:
    qty: int
    avg: float
    high: float
    entry_day: int
    partial_done: bool = False
    be_armed: bool = False


@dataclass
class Book:
    cash: float
    market: str
    stocks: set = field(default_factory=set)
    pos: Dict[str, Pos] = field(default_factory=dict)
    trades: int = 0
    wins: int = 0
    closed: int = 0
    turnover: float = 0.0

    def buy(self, code: str, price: float, amount: float, day: int) -> int:
        c = COSTS[self.market]
        unit = price * (1 + c.buy)
        qty = int(min(amount, self.cash) // unit)
        if qty <= 0:
            return 0
        self.cash -= qty * unit
        p = self.pos.get(code)
        if p:
            p.avg = (p.avg * p.qty + price * qty) / (p.qty + qty)
            p.qty += qty
        else:
            self.pos[code] = Pos(qty, price, price, day)
        self.trades += 1
        self.turnover += qty * price
        return qty

    def sell(self, code: str, price: float, qty: Optional[int] = None) -> None:
        p = self.pos.get(code)
        if not p:
            return
        qty = p.qty if qty is None else min(qty, p.qty)
        c = COSTS[self.market]
        fee = c.sell + (c.sell_tax_stock if code in self.stocks else 0.0)
        self.cash += qty * price * (1 - fee)
        self.trades += 1
        self.turnover += qty * price
        if qty >= p.qty:
            self.closed += 1
            if price > p.avg:
                self.wins += 1
            del self.pos[code]
        else:
            p.qty -= qty

    def equity(self, px: Dict[str, float]) -> float:
        return self.cash + sum(p.qty * px.get(c, p.avg) for c, p in self.pos.items())


def metrics(curve: pd.Series, book: Book, exposure: List[float]) -> Dict:
    curve = curve.dropna()
    if len(curve) < 2:
        return {}
    days = (curve.index[-1] - curve.index[0]).days
    years = max(days / 365.25, 1e-9)
    total = curve.iloc[-1] / curve.iloc[0] - 1
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    dd = (curve / curve.cummax() - 1).min()
    rets = curve.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0
    yearly = curve.resample("YE").last().pct_change()
    yearly.iloc[0] = curve.resample("YE").last().iloc[0] / curve.iloc[0] - 1
    return {
        "total_pct": round(total * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "mdd_pct": round(dd * 100, 1),
        "sharpe": round(sharpe, 2),
        "calmar": round((cagr / abs(dd)) if dd < 0 else 0.0, 2),
        "trades": book.trades,
        "win_rate_pct": round(book.wins / book.closed * 100, 1) if book.closed else None,
        "avg_exposure_pct": round(sum(exposure) / len(exposure) * 100, 1) if exposure else 0.0,
        "turnover_x_per_year": round(book.turnover / curve.mean() / years, 1),
        "yearly_pct": {str(k.year): round(v * 100, 1) for k, v in yearly.items()},
    }


# ---------------------------------------------------------------------------
# 공통: 날짜 정렬 패널
# ---------------------------------------------------------------------------
def build_panel(data: Dict[str, pd.DataFrame], bench: str) -> Tuple[pd.DatetimeIndex, Dict[str, pd.DataFrame]]:
    idx = data[bench].index
    panel = {c: df.reindex(idx) for c, df in data.items()}
    return idx, panel


def _closes_upto(df: pd.DataFrame, i: int, n: int = 260) -> List[float]:
    s = df["Close"].iloc[max(0, i - n + 1):i + 1].dropna()
    return s.tolist()


# ---------------------------------------------------------------------------
# 전략 1: 현행 v6.1 근사
# ---------------------------------------------------------------------------
def run_current(data, market, start, capital, universe: List[str], stocks: set, bench: str,
                risk: Dict) -> Dict:
    idx, panel = build_panel(data, bench)
    book = Book(cash=capital, market=market, stocks=stocks)
    stop = float(risk.get("stop_loss_pct", -12.0))
    act = float(risk.get("trailing_stop_activation_pct", 10.0))
    drop = float(risk.get("trailing_stop_drop_pct", 6.0))
    be_trig = float(risk.get("breakeven_trigger_pct_us" if market == "US" else "breakeven_trigger_pct_kr_stock", 3.0))
    be_buf = float(risk.get("breakeven_buffer_pct", 0.2))
    min_inv = 50.0 if market == "US" else 200_000.0
    max_inv = 2000.0 if market == "US" else 2_000_000.0
    curve, exposure = {}, []
    stopped_today: Dict[str, int] = {}
    start_i = max(idx.searchsorted(pd.Timestamp(start)), 130)

    for i in range(start_i, len(idx)):
        px = {c: float(panel[c]["Close"].iloc[i]) for c in universe
              if c in panel and not pd.isna(panel[c]["Close"].iloc[i])}
        # --- 청산 (장중 60초 감시를 일중 고/저가로 근사) ---
        for c in list(book.pos):
            df = panel[c]
            if pd.isna(df["Close"].iloc[i]):
                continue
            p = book.pos[c]
            o, h, lo, cl = (float(df[k].iloc[i]) for k in ("Open", "High", "Low", "Close"))
            stop_px = p.avg * (1 + stop / 100)
            if lo <= stop_px:
                book.sell(c, min(o, stop_px)); stopped_today[c] = i; continue
            p.high = max(p.high, h)
            if (p.high / p.avg - 1) * 100 >= be_trig:
                p.be_armed = True
            if (p.high / p.avg - 1) * 100 >= act:
                if not p.partial_done and p.qty >= 2:
                    book.sell(c, p.avg * (1 + act / 100), p.qty // 2); p.partial_done = True
                trail_px = p.high * (1 - drop / 100)
                if c in book.pos and lo <= trail_px:
                    book.sell(c, min(o, trail_px)); continue
            elif p.be_armed and lo <= p.avg * (1 + be_buf / 100):
                book.sell(c, min(o, p.avg * (1 + be_buf / 100))); continue
            ma20 = df["Close"].iloc[i - 19:i + 1].mean()
            if c in book.pos and cl < ma20 * (1 - 0.005) and (cl / p.avg - 1) * 100 <= -0.5:
                book.sell(c, cl); continue
        eq = book.equity(px)
        # --- 매수 (전 후보 매일 재평가) ---
        n_targets = len(px)
        for c in universe:
            if c not in px or stopped_today.get(c) == i:
                continue
            df = panel[c]
            closes = _closes_upto(df, i, 70)
            if len(closes) < 61:
                continue
            price = px[c]
            p = book.pos.get(c)
            ctx = TickerContext(
                ticker=c, market=market, price=price,
                ma5=sum(closes[-5:]) / 5, ma10=sum(closes[-10:]) / 10,
                ma20=sum(closes[-20:]) / 20, ma60=sum(closes[-60:]) / 60,
                prev_close=closes[-2], day_low=float(df["Low"].iloc[i]), day_high=float(df["High"].iloc[i]),
                gap_pct=(float(df["Open"].iloc[i]) / closes[-2] - 1) * 100,
                consec_decline_pct=max(0.0, (closes[-4] - price) / closes[-4] * 100),
                is_leveraged=c in US_3X,
                holding_qty=p.qty if p else 0, holding_avg_price=p.avg if p else 0.0,
                position_value=(p.qty * price) if p else 0.0,
                max_position_value=eq * 0.25,
            )
            d = evaluate_buy(ctx, PortfolioContext(available_cash=book.cash))
            if d.action != "buy":
                continue
            slots = max(1, min(n_targets, int(book.cash // min_inv)))
            amt = min(book.cash * 0.30, book.cash / slots)
            amt = max(min_inv, min(max_inv, amt)) * d.size_multiplier
            if amt < price <= book.cash:
                amt = price
            book.buy(c, price, amt, i)
        eq = book.equity(px)
        curve[idx[i]] = eq
        exposure.append(1 - book.cash / eq if eq > 0 else 0)
    return metrics(pd.Series(curve), book, exposure)


# ---------------------------------------------------------------------------
# 전략 2: 모멘텀 로테이션 (modules/momentum_rotation — 봇과 같은 함수)
# ---------------------------------------------------------------------------
def run_rotation(data, market, start, capital, universe: List[str], stocks: set, bench: str,
                 slots: int = 2, buffer: int = 3, rebalance_days: int = 20,
                 market_filter: bool = True, close_stop_pct: Optional[float] = None,
                 daily_risk_check: bool = True, defensive: Optional[List[str]] = None,
                 end: Optional[str] = None) -> Dict:
    defensive = [c for c in (defensive or []) if c in data]
    if end:
        data = {k: v[v.index < pd.Timestamp(end)] for k, v in data.items()}
    idx, panel = build_panel(data, bench)
    book = Book(cash=capital, market=market, stocks=stocks)
    min_slot = 50.0 if market == "US" else 200_000.0
    curve, exposure = {}, []
    start_i = max(idx.searchsorted(pd.Timestamp(start)), 130)
    pending: Optional[Tuple[List[Tuple[str, str]], List[str], int]] = None
    last_rebal = -10 ** 9

    for i in range(start_i, len(idx)):
        opx = {c: float(panel[c]["Open"].iloc[i]) for c in universe + defensive
               if c in panel and not pd.isna(panel[c]["Open"].iloc[i])}
        px = {c: float(panel[c]["Close"].iloc[i]) for c in universe + defensive
              if c in panel and not pd.isna(panel[c]["Close"].iloc[i])}
        # --- t 일 시가: 전일 결정 체결 ---
        if pending:
            sells, targets, n_slots = pending
            for c, _r in sells:
                if c in book.pos and c in opx:
                    book.sell(c, opx[c])
            eq = book.equity(opx)
            want = [t for t in targets if t not in book.pos and t in opx]
            if want:
                per = eq / n_slots
                for t in want:
                    book.buy(t, opx[t], min(per, book.cash), i)
            pending = None

        # --- t 일 종가: 결정 ---
        bench_closes = _closes_upto(panel[bench], i, 200)
        risk_on = market_risk_on(bench_closes) if market_filter else True
        eq = book.equity(px)
        rebalance_due = (i - last_rebal) >= rebalance_days
        stop_sells = []
        if close_stop_pct is not None:
            for c, p in book.pos.items():
                if c in px and (px[c] / p.avg - 1) * 100 <= close_stop_pct:
                    stop_sells.append((c, "종가손절"))
        # 봇과 동일: risk-off 동안은 매일 판단한다 (방어자산 편입·해제 포함)
        risk_exit = daily_risk_check and market_filter and not risk_on and (book.pos or defensive)
        if rebalance_due or risk_exit or stop_sells:
            feats = [compute_features(c, _closes_upto(panel[c], i)) for c in universe if c in px]
            n_slots = fillable_slots(eq, min_slot, slots)
            plan = plan_rotation(feats, [c for c in book.pos if c not in defensive], risk_on, n_slots, buffer)
            # 봇(run_rotation_session)과 동일: 리밸런싱일이 아니면 risk-off 청산과
            # 종가손절만 한다. 순위/추세 이탈 매도는 리밸런싱일에만.
            sells = list(plan.sell) if (rebalance_due or not risk_on) else []
            for s in stop_sells:
                if s[0] not in [x[0] for x in sells]:
                    sells.append(s)
            targets = plan.targets if rebalance_due else [t for t in plan.targets if t in book.pos]
            targets = [t for t in targets if t not in [s[0] for s in stop_sells]]
            if defensive and (rebalance_due or not risk_on):   # 봇 _rotation_session 과 같은 규칙
                dt = defensive_targets({d: _closes_upto(panel[d], i) for d in defensive if d in px},
                                       n_slots - len(targets))
                sells += [(d, "방어자산 해제") for d in book.pos if d in defensive and d not in dt]
                targets = targets + dt
            if rebalance_due:
                last_rebal = i
            if sells or any(t not in book.pos for t in targets):
                pending = (sells, targets, n_slots)
        curve[idx[i]] = eq
        exposure.append(1 - book.cash / eq if eq > 0 else 0)
    return metrics(pd.Series(curve), book, exposure)


def run_buy_hold(data, start, capital, bench, market) -> Dict:
    df = data[bench]
    df = df[df.index >= pd.Timestamp(start)]
    book = Book(cash=capital, market=market)
    book.buy(bench, float(df["Open"].iloc[0]), capital, 0)
    curve = df["Close"] * book.pos[bench].qty + book.cash
    return metrics(curve, book, [1.0])


# ---------------------------------------------------------------------------
def _load_risk() -> Dict:
    with open(os.path.join(ROOT, "user_config.json"), encoding="utf-8") as f:
        return json.load(f).get("risk_management", {})


def _load_rotation(market: str) -> Dict:
    with open(os.path.join(ROOT, "user_config.json"), encoding="utf-8") as f:
        return (json.load(f).get("momentum_rotation") or {}).get(market.lower(), {})


def run_operational(data, market, start, capital, universe, end=None, defensive=True) -> Dict:
    """운영 설정(user_config.json momentum_rotation) 그대로 돌린다.

    AGENTS 규칙 8: 검증 스크립트에 값을 복사해 두면 운영 설정을 바꿔도 검증은
    옛날 값으로 통과한다. 그래서 여기서는 설정을 **읽는다**.
    """
    rc = _load_rotation(market)
    bench = rc.get("benchmark", KR_BENCH if market == "KR" else US_BENCH)
    return run_rotation(data, market, start, capital, universe, set(), bench,
                        int(rc.get("slots", 2)), int(rc.get("sell_buffer", 3)),
                        int(rc.get("rebalance_days", 10)),
                        market_filter=bool(rc.get("market_filter", market == "KR")),
                        close_stop_pct=rc.get("close_stop_pct"),
                        defensive=rc.get("defensive") if defensive else None, end=end)


def run_market(market: str, start: str, refresh: bool, source: str = "auto") -> Dict[str, Dict]:
    risk = _load_risk()
    if market == "KR":
        operational = _operational_universe("KR")
        etfs = list(dict.fromkeys(operational + list(KR_ETFS)))
        stocks, bench, capital = list(KR_STOCKS), KR_BENCH, 2_700_000.0
        data = load_prices(etfs + stocks + _load_rotation("KR").get("defensive", []), "KR", start, refresh, source)
        if bench not in data:
            raise RuntimeError(
                f"KR 백테스트 벤치마크 {bench} 데이터가 없습니다. "
                "Yahoo rate limit/네트워크 또는 data_cache를 확인하세요."
            )
        etfs = [c for c in etfs if c in data]
        stocks = [c for c in stocks if c in data]
        cur_univ = etfs + stocks
        variants = {
            "B&H KODEX200": lambda: run_buy_hold(data, start, capital, bench, market),
            "현행 v6.1 (전 후보 DCA+장중손절)": lambda: run_current(data, market, start, capital, cur_univ, set(stocks), bench, risk),
            "현행 v6.1 ETF만": lambda: run_current(data, market, start, capital, etfs, set(), bench, risk),
            "★ 운영설정 (user_config)": lambda: run_operational(data, market, start, capital, etfs),
            "운영설정 − 방어자산 (v7.0)": lambda: run_operational(data, market, start, capital, etfs, defensive=False),
            "로테이션 top2 R20 +시장필터": lambda: run_rotation(data, market, start, capital, etfs, set(), bench, 2, 3, 20),
            "로테이션 top2 R10 +시장필터": lambda: run_rotation(data, market, start, capital, etfs, set(), bench, 2, 3, 10),
            "로테이션 top3 R20 +시장필터": lambda: run_rotation(data, market, start, capital, etfs, set(), bench, 3, 4, 20),
            "로테이션 top2 R20 필터없음": lambda: run_rotation(data, market, start, capital, etfs, set(), bench, 2, 3, 20, market_filter=False),
            "로테이션 top2 R20 +필터 +종가손절-10": lambda: run_rotation(data, market, start, capital, etfs, set(), bench, 2, 3, 20, close_stop_pct=-10.0),
        }
    else:
        operational = _operational_universe("US")
        u1 = list(dict.fromkeys(operational + list(US_1X)))
        u3, bench, capital = list(US_3X), US_BENCH, 2000.0
        data = load_prices(u1 + u3 + _load_rotation("US").get("defensive", []), "US", start, refresh, source)
        if bench not in data:
            raise RuntimeError(
                f"US 백테스트 벤치마크 {bench} 데이터가 없습니다. "
                "Yahoo rate limit/네트워크 또는 data_cache를 확인하세요."
            )
        cur_univ = [c for c in u3 + u1 if c in data]
        u1 = [c for c in u1 if c in data]
        variants = {
            "B&H SPY": lambda: run_buy_hold(data, start, capital, bench, market),
            "현행 v6.1 (3x+1x DCA+장중손절)": lambda: run_current(data, market, start, capital, cur_univ, set(), bench, risk),
            "★ 운영설정 (user_config)": lambda: run_operational(data, market, start, capital, u1),
            "로테이션 1x top2 R10 필터없음": lambda: run_rotation(data, market, start, capital, u1, set(), bench, 2, 3, 10, market_filter=False),
            "로테이션 1x top2 R20 +시장필터": lambda: run_rotation(data, market, start, capital, u1, set(), bench, 2, 3, 20),
            "로테이션 1x top1 R20 +시장필터": lambda: run_rotation(data, market, start, capital, u1, set(), bench, 1, 2, 20),
            "로테이션 3x+1x top2 R20 +시장필터": lambda: run_rotation(data, market, start, capital, cur_univ, set(), bench, 2, 3, 20),
            "로테이션 1x top2 R20 필터없음": lambda: run_rotation(data, market, start, capital, u1, set(), bench, 2, 3, 20, market_filter=False),
        }
    results = {}
    for name, fn in variants.items():
        results[name] = fn()
    return results


def robustness(market: str, refresh: bool, source: str) -> None:
    """[v8.0] 과적합 점검 — 고른 기간(train)과 안 본 기간(test), 시작 시점 6개.

    한 구간 최고값이 아니라 **안 본 구간에서도, 어느 시점에 시작해도** 무너지지 않는지를 본다
    (AGENTS v7.0 규칙 2). 운영설정과 '방어자산 없는 v7.0'을 나란히 찍는다.
    """
    rc = _load_rotation(market)
    uni = list(dict.fromkeys(_operational_universe(market) + list(KR_ETFS if market == "KR" else US_1X)))
    cap = 2_700_000.0 if market == "KR" else 2000.0
    data = load_prices(uni + rc.get("defensive", []), market, "2017-01-01", refresh, source)
    uni = [c for c in uni if c in data]
    windows = [("train 2017-21", "2017-01-01", "2022-01-01"), ("test 2022-", "2022-01-01", None)]
    windows += [(f"start {y}", f"{y}-01-01", None) for y in range(2017, 2023)]
    print(f"\n=== {market} robustness (운영설정 vs 방어자산 없음) ===")
    print(f"{'구간':<16}{'운영 CAGR':>10}{'MDD':>8}{'Sharpe':>8}   {'v7.0 CAGR':>10}{'MDD':>8}{'Sharpe':>8}")
    for name, s, e in windows:
        a = run_operational(data, market, s, cap, uni, end=e)
        b = run_operational(data, market, s, cap, uni, end=e, defensive=False)
        print(f"{name:<16}{a['cagr_pct']:>9.1f}%{a['mdd_pct']:>7.1f}%{a['sharpe']:>8.2f}   "
              f"{b['cagr_pct']:>9.1f}%{b['mdd_pct']:>7.1f}%{b['sharpe']:>8.2f}")


def print_table(market: str, start: str, results: Dict[str, Dict]) -> None:
    print(f"\n=== {market}  ({start} ~ 최신, 실제 일봉·비용 포함) ===")
    hdr = f"{'전략':<36}{'CAGR':>7}{'MDD':>8}{'Sharpe':>8}{'Calmar':>8}{'노출':>7}{'거래':>6}{'승률':>7}{'회전/년':>8}"
    print(hdr)
    for name, m in results.items():
        if not m:
            continue
        wr = f"{m['win_rate_pct']:.0f}%" if m.get("win_rate_pct") is not None else "-"
        print(f"{name:<36}{m['cagr_pct']:>6.1f}%{m['mdd_pct']:>7.1f}%{m['sharpe']:>8.2f}{m['calmar']:>8.2f}"
              f"{m['avg_exposure_pct']:>6.0f}%{m['trades']:>6}{wr:>7}{m['turnover_x_per_year']:>8.1f}")
    years = sorted({y for m in results.values() if m for y in m["yearly_pct"]})
    print("\n연도별 수익률(%)")
    print(f"{'전략':<36}" + "".join(f"{y:>7}" for y in years))
    for name, m in results.items():
        if not m:
            continue
        print(f"{name:<36}" + "".join(f"{m['yearly_pct'].get(y, float('nan')):>7.1f}" for y in years))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["KR", "US", "ALL"], default="ALL")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 다시 다운로드")
    ap.add_argument("--json", help="결과를 JSON 으로 저장")
    ap.add_argument("--source", choices=["auto", "toss", "yfinance"], default="auto",
                    help="일봉 출처 (auto = TOSS_CLIENT_ID 있으면 토스)")
    ap.add_argument("--robustness", action="store_true", help="운영설정 train/test·시작연도 점검")
    args = ap.parse_args()
    markets = ["KR", "US"] if args.market == "ALL" else [args.market]
    if args.robustness:
        for mk in markets:
            robustness(mk, args.refresh, args.source)
        return 0
    out = {}
    for mk in markets:
        res = run_market(mk, args.start, args.refresh, args.source)
        print_table(mk, args.start, res)
        out[mk] = res
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
