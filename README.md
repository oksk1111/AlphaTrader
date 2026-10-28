# Alphatrader

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/web-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![Broker](https://img.shields.io/badge/broker-토스증권_Open_API-0064FF)](https://developers.tossinvest.com)
[![Telegram](https://img.shields.io/badge/notify-Telegram-2CA5E0)](#)

미국/국내 ETF 자동매매 봇. **듀얼 모멘텀 로테이션**으로 강한 ETF 2개에 집중하고, 약세장에서는
채권·금(KR) 또는 현금으로 빠지며, 판단은 완성된 일봉 종가로만 한다.
증권사는 **토스증권 Open API**(REST + 웹소켓), 운영 상태는 FastAPI 대시보드로 본다.

> **v8.0 (2026-10-29)** — 한국투자증권 API 를 제거하고 토스증권 단일 브로커로 옮겼다
> (`modules/brokers`). 실주문 여부는 `EXECUTION_MODE` 하나로 정한다 — **기본 `shadow` 는 판단·알림은
> 전부 돌고 주문만 보내지 않는다**, `live` 가 실주문. 웹소켓은 실시간 체결가로 주문 단가를 잡고 주문
> 이벤트로 체결을 즉시 확인한다(접수 ≠ 체결). 주식 ETF 로 못 채운 KR 슬롯은 추세가 살아 있는 국고채·금으로
> 채운다 — 사전에 정한 채택 규칙(안 본 2022~ 구간 Sharpe 개선 · MDD 악화 2%p 이내 · 시작연도 6개
> 최소 Sharpe 유지)을 통과한 것만 넣었다. 근거: `docs/주식 자동 매매 기획.md` 문서 1-D.

## 전략

| | 🇰🇷 KR | 🇺🇸 US |
|---|---|---|
| 유니버스 | ETF 17종 (`user_config.json`) | SPY · QQQ · SMH · IWM · XLK (3x 레버리지 없음) |
| 순위 | 0.55·z(R60) + 0.45·z(R120), 추세조건 close > MA20 > MA60 | 같음 |
| 보유 | 상위 2개, 보유 종목은 3위 밖으로 밀려야 매도 | 같음 |
| 리밸런싱 | 10 거래일 | 10 거래일 |
| 시장필터 | KODEX200 ≥ MA120 이고 MA120 상승 → 아니면 risk-off | 없음 (V자 반등 휩쏘) |
| 빈 슬롯 | 방어자산(국고채10년 148070 · 금 132030) — 자기 추세(> MA120, R120 > 0)가 있을 때만, 없으면 현금 | 현금 |
| 청산 | 종가 기준 −15% 비상손절 (폐장 20분 전). 장중 꼬리는 매도 사유가 아니다 | 같음 |
| 자금 | 원화 매수가능금액 | USD 매수가능금액 (자동환전 없음, 원화를 끌어오지 않는다) |

실데이터 백테스트 (2017-01 ~ 2026-09, 비용 포함, `scripts/backtest_real.py`):

| | CAGR | MDD | Sharpe |
|---|---|---|---|
| KR v8.0 운영설정 | **22.6%** | **−21.1%** | **1.15** |
| KR v7.0 (방어자산 없음) | 20.6% | −21.1% | 1.09 |
| KR B&H KODEX200 | 18.0% | −40.8% | 0.79 |
| US 운영설정 (5종목) | 13.4% | −23.6% | 0.90 |
| US B&H SPY | 14.9% | −33.1% | 0.87 |

⚠️ 생존편향(현재 상장 ETF 만)이 있다. 절대 수익률보다 같은 유니버스 안의 상대 비교를 보라.
⚠️ US 5종목(IWM·XLK 추가, 10/07 — 소액 계좌 1주 판정용)은 2021년 이후 시작 구간에서 3종목보다 약하다
(2021- Sharpe 0.43 vs 0.73). 계좌가 커지면 재검토할 것.

## 빠른 시작

```bash
pip install -r requirements.txt
cp .env.example .env      # TOSS_CLIENT_ID / TOSS_CLIENT_SECRET, EXECUTION_MODE, GEMINI_API_KEY, TELEGRAM_* …
python run_bot.py                                            # 봇 (EXECUTION_MODE=shadow 면 주문 미전송)
python -m uvicorn web.app:app --host 127.0.0.1 --port 8501  # 대시보드
python -m modules.shadow_collector                           # 실시간 데이터 수집 (주문 안 함)
```

토스증권 WTS → 설정 → Open API 에서 키를 발급하고, **실행하는 머신의 공인 IP 를 허용 IP 에
등록**해야 한다. 클라이언트당 유효 토큰은 1개라, 같은 키로 다른 곳에서 실행하면 서로의 토큰을
무효화한다. 운영 서버 배포와 GitHub Secrets 는 [`docs/DEPLOY.md`](docs/DEPLOY.md).

## 설정 (`user_config.json`)

- `strategy`: `momentum_rotation` (운영). `aggressive_dca` 로 바꾸면 v6.x 점수 기반 DCA 경로로 돌아간다.
- `momentum_rotation.{kr,us}`: `universe` · `benchmark` · `slots` · `sell_buffer` · `rebalance_days` ·
  `market_filter` · `close_stop_pct` · `min_slot_value` · `defensive`(KR 만)
- `risk_management` / `aggressive_dca`: DCA 경로 전용 (AGENTS.md 의 v6.x 섹션)

## 검증

```bash
pytest tests/ -q --ignore=tests/test_portfolio_manager.py   # 외부 API 없이 실행 (응답 대역)
python scripts/backtest_real.py                 # 실데이터 백테스트 — KR·US 전 변형
python scripts/backtest_real.py --robustness    # 운영설정 과적합 점검: train 2017-21 / test 2022- / 시작연도 6개
python scripts/verify_strategy_loop.py --sessions 80 --seed 42 --seeds 5 --quiet   # CI 게이트 (DCA 경로)
```

`backtest_real.py` 는 `TOSS_CLIENT_ID` 가 있으면 토스 일봉을, 없으면 yfinance 를 쓴다
(`--source toss|yfinance`). 둘 다 `data_cache/` 에 캐시한다.

| 테스트 | 잠그는 것 |
|---|---|
| `test_v80_toss.py` | 호환 계층: 보유 0 ≠ 조회 실패, KR 총평가 = 주식 + 예수금, US +1% 지정가, 접수 ≠ 체결, 부분체결, shadow 표시, 실시간 fast path |
| `test_toss_broker.py` · `test_toss_stream.py` | TossBroker(토큰 공유 · shadow/live · submit_and_wait) · TossStream(구독 · 프레임) |
| `test_intraday_strategy.py` · `test_walk_forward_intraday.py` · `test_shadow_config.py` | 장중 점수 엔진 · walk-forward gate · shadow 기본값 |
| `test_v70_rotation.py` | 로테이션 판단·배선, 방어자산 슬롯, 시장 간 자금 분리, 관측 |
| `test_v61_observability.py` | 로그 파일 주인 · 단일 시계 · heartbeat 생존 판정 · 소액계좌 1주 바닥 |
| `test_v60_*.py` | DST · 점수 판단 · 낙폭 사다리 · 장중 재평가 배선 |

## 운영 관측

- `GET /api/status` — `bot_status` · `market_status` · `heartbeat_age_sec` · `session_active` · `session_outcome` ·
  `execution_mode` · `stream_connected` · `shadow_sessions_collected`
- `database/trading.log` (봇) / `database/dashboard.log` (대시보드) — 자정 회전, 줄마다 `[bot]`/`[dash]`
- `database/heartbeat.json` — 감시 스크립트가 나이로 생존 판정 (장중 5분 정지 = 좀비 → 재기동)
- 연속 무매수 3세션 경고 / 7세션 긴급 (Telegram, 지배적 차단 사유 + 조치 힌트)

## 구조

```text
run_bot.py                 메인 루프 · 로테이션 세션 · safe_sell
modules/
  brokers/                 v8.0 — models(정규 모델) · toss(TossBroker) · toss_stream(TossStream) · compat(run_bot 호환 계층)
  shadow_collector.py      실시간 데이터·장중 신호 수집 (별도 프로세스, 주문 안 함)
  intraday_strategy.py     장중 ETF 점수 엔진 — live_enabled=false (20거래일 + walk-forward gate 전까지)
  momentum_rotation.py     순위 · 시장필터 · 슬롯 · 방어자산 (봇과 백테스트가 같은 함수)
  market_clock.py          유일한 시계 (DST · 휴장일)
  decision_engine.py / risk_state.py / health_monitor.py
  account_manager.py / profit_tracker.py / logger.py / telegram_notifier.py
  multi_llm.py / sector_news.py / *_analyst.py    AI 뉴스 분석 (DCA 경로)
scripts/  backtest_real.py · verify_strategy_loop.py · run_walk_forward.py
web/      FastAPI 대시보드          deployment/  deploy.sh · setup_cloud.sh · systemd
tests/    회귀 테스트               docs/        기획서 · 배포 · 용어
```

## 문서

- [`AGENTS.md`](AGENTS.md) — 개발 지침과 버전별 규칙 (코드를 고치기 전에 읽을 것)
- [`docs/주식 자동 매매 기획.md`](docs/주식%20자동%20매매%20기획.md) — 기획서 · 버전별 근거(문서 1-A ~ 1-D) · 이력
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — CI/CD · GitHub Secrets · 서버 설치 · 토스 에러 대응
- [`docs/baseknowledge.md`](docs/baseknowledge.md) — 퀀트 용어 입문
- [`docs/TELEGRAM_SETUP.md`](docs/TELEGRAM_SETUP.md) · [`docs/BACKTEST_AUTOMATION.md`](docs/BACKTEST_AUTOMATION.md)

## 버전

| 버전 | 날짜 | 요지 |
|---|---|---|
| v8.0 | 2026-10-29 | 한국투자증권 제거 · 토스 단일 브로커(shadow/live) · 실시간 집행/체결 확인 · KR 방어자산 슬롯 · 과적합 점검 |
| v8.0-shadow | 2026-10-28 | 토스 어댑터·실시간 shadow 수집·장중 점수 엔진 (KIS 실주문 유지 1단계) |
| v7.0.3 | 2026-10-07 | 조용한 세션 제거 — 텔레그램 세션 요약·체결·장마감 알림, US IWM·XLK 추가 |
| v7.0 | 2026-10-01 | 무엇을 들고 있나 — 21종목 DCA → ETF 상위 2 로테이션 (KR CAGR 4.7% → 20.9%) |
| v6.1 | 2026-09-10 | 계기판 먼저 — 로그 파일 · 시계 · 생존 판정 · 소액계좌 0주 |
| v6.0 | 2026-09-08 | 이진 게이트 → 점수 · 장중 재평가 · DST · 낙폭 사다리 |
| ≤ v5 | ~2026-07 | 기획서 문서 2 참고 |

## ⚠️ Disclaimer

정보 제공 및 개인 자동화 목적이다. 투자 판단과 손익의 책임은 사용자에게 있다.
토스증권 Open API 는 모의투자 환경이 없다 — `EXECUTION_MODE=shadow` 로 판단·알림을 먼저 확인하고,
`live` 전환 후 첫 세션은 소액으로 로그를 확인하라.
