# Alphatrader

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/web-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![KIS API](https://img.shields.io/badge/broker-KIS_API-orange)](#)
[![Telegram](https://img.shields.io/badge/notify-Telegram-2CA5E0)](#)

미국/국내 주식 자동매매 시스템입니다.
룰 기반 전략(VBO, MA)과 AI 보조 분석을 결합해 매수/매도 의사결정을 자동화하고, FastAPI 대시보드로 운영 상태를 모니터링합니다.

빠른 이동: [Key Features](#-key-features) · [Quick Start](#-quick-start) · [Configuration](#-configuration) · [Backtest Automation](#-backtest-automation) · [Dashboard](#️-dashboard)

## ✨ Key Features

| 모듈 | 기능 | 설명 |
|------|------|------|
| Trading Engine | 실시간 웹소켓 + 자동매매 | 장 상태 감지 및 미국 시장 실시간 웹소켓(WebSocket) 스트리밍 가격 수신을 통한 초저지연 실시간 시세 감시 및 고성능 변동성 돌파 구현 |
| Strategy | 다중 전략 | `day`, `swing`, `dca` 전략과 `safe/risky` 모드 지원 |
| Risk Control | 리스크 관리 | 손절, 트레일링 스탑, 갭다운/연속하락/포트폴리오 드로다운 방어, **시장가→지정가 fallback 매도** |
| Rebound Trigger | 변동성 반등 매수 | 큰 하락 후 당일 반등 시 50% 수량으로 역방향 진입 (v2.3) |
| Partial Take-Profit | 1차 부분 익절 | Trailing 활성가 도달 시 50% 청산 + 잔량 trailing (v2.4) |
| Breakeven Stop | 본전 스탑 | 고점 +3%/+4% 도달 후 손절선을 매수가 +0.2% 위로 끌어올림 (v2.5) |
| Correlation Cap | 상관 그룹 한도 | 동일 섹터 그룹 동시 보유 최대 2종목 (v2.5) |
| Losing Streak Throttle | 일일 손실 회로차단 | 손절 3건 또는 누적 -3% 시 신규 매수 즉시 중단 (v2.5) |
| ATR Dynamic Stop | 변동성 적응 손절 | 14일 ATR 기반으로 종목별 손절폭 동적 보강 (v2.5) |
| AI Assist | 시장 보조 분석 | 뉴스 기반 위험도 판단 및 매수 제한(페르소나 반영) |
| Dynamic Portfolio | 동적 포트폴리오 | 고품질 ETF 풀 중 모멘텀/안정성이 우수한 종목을 시스템이 주기적으로 자동 필터링 및 교체(삭제) |
| AI Consensus Policy | 설정 기반 합의 | 쿼럼/매수비율/CRASH veto/동률처리를 설정으로 제어 |
| Auto Strategy | 자동 전략 전환 | 시장/자산/포지션 상태 기반 전략/모드/페르소나 자동 최적화 |
| Dashboard | 운영 관제 | 총자산, 수익률, 보유종목, 최근 주문/로그를 웹에서 확인 |
| Notifications | 알림 전송 | Telegram 연동으로 중요 이벤트 전달 |

## 🧱 Tech Stack

| 구분 | 내용 |
|------|------|
| Language | Python 3.10+ |
| Broker API | KIS Open API |
| AI | Gemini + Multi-LLM adapters |
| Web | FastAPI + Jinja2 + Vanilla JS/CSS |
| Data | JSON 기반 캐시/히스토리 (`database/`) |

## 🚀 Quick Start

### 1. 설치

```bash
pip install -r requirements.txt
```

### 2. 환경 변수 설정

`.env` 파일에 최소 아래 값을 설정하세요.

```env
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=...
KIS_PRODUCT_CODE=...
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### 3. 사용자 설정 확인

`user_config.json` 예시:

```json
{
  "auto_strategy": true,
  "trading_mode": "safe",
  "strategy": "dca",
  "persona": "aggressive",
  "theme_mode": "light"
}
```

### 4. 봇 실행

```bash
python run_bot.py
```

### 5. 대시보드 실행

```bash
python web/app.py
```

기본 접속: `http://127.0.0.1:8501`

참고: `web/app.py`(FastAPI + Jinja2)이 기본 대시보드이며, `dashboard.py`는 레거시 Streamlit 대시보드입니다.

## ⚙️ Configuration

주요 설정 파일:

- `user_config.json`: 전략/모드/페르소나, DCA, 리스크, 알림 설정
- `config.py`: 기본 상수/환경 의존 설정
- `database/*.json`: 계좌 캐시, 스냅샷, 전략 히스토리

핵심 파라미터:

- `strategy`: `day` | `swing` | `dca`
- `trading_mode`: `safe` | `risky`
- `persona`: `aggressive` | `neutral` | `conservative`
- `risk_management`: 손절/트레일링/드로다운 임계값 및 **반등 매수** 옵션
  - `stop_loss_pct` (기본 **-5.0%**), `trailing_stop_activation_pct` (**5.0%**), `trailing_stop_drop_pct` (**3.0%**)
  - `gap_down_threshold_pct` (**5.0%**), `consecutive_decline_pct` (**5.0%**), `portfolio_drawdown_pct` (**7.0%**)
  - `rebound_buy_enabled`, `rebound_drop_threshold_pct` (**5.0%**), `rebound_intraday_bounce_pct` (**1.0%**), `rebound_max_buys_per_session` (**1**)
  - v2.4: `partial_tp_enabled` (**true**), `partial_tp_ratio` (**0.5**)
  - **v2.5 신규**:
    - `breakeven_enabled` (**true**), `breakeven_trigger_pct_us` (**3.0**), `breakeven_trigger_pct_kr_stock` (**4.0**), `breakeven_buffer_pct` (**0.2**)
    - `correlation_cap_enabled` (**true**), `correlation_max_per_group` (**2**), `correlation_groups` (TQQQ/TECL/.., 005930/000660 기본)
    - `losing_streak_enabled` (**true**), `losing_streak_max_stops` (**3**), `losing_streak_daily_pnl_pct` (**-3.0**)
    - `atr_dynamic_stop_enabled` (**true**), `atr_period` (**14**), `atr_stop_multiplier` (**2.0**)
- `dca_settings`: 일간 투자비중/매수상한/세션 매수 횟수
  - `daily_investment_pct` (기본 **30%**), `max_investment_usd` (**$1,500**), `max_buys_per_session` (**5**)

LLM 합의 정책 파라미터:

- `llm_consensus.crash_veto`: CRASH 단일 veto 적용 여부
- `llm_consensus.min_successful_llms`: 최소 응답 LLM 수(쿼럼)
- `llm_consensus.required_buy_ratio`: 매수 승인 찬성 비율 임계값
- `llm_consensus.unknown_fallback_hold`: 전체 실패 시 관망 고정 여부
- `llm_consensus.tie_breaker`: 동률 처리(`persona`/`buy`/`hold`)

예시:

```json
{
  "llm_consensus": {
    "crash_veto": true,
    "min_successful_llms": 2,
    "required_buy_ratio": 0.6,
    "unknown_fallback_hold": true,
    "tie_breaker": "persona"
  }
}
```

## ⏱️ Operations & Scheduling

`auto_restart_bot.sh` 기준 기본 운영 주기:

| 작업 | 주기 | 기준 시간 |
|------|------|-----------|
| 봇 프로세스 감시/재시작 | 장 운영 중 30초 간격 | KST |
| 대시보드 감시/재시작 | 상시 | KST |
| 일일 리포트 발송 | 매일 16시 1회 | KST |
| 주간 백테스트 리포트 | 일요일 07시 1회 | KST |

관련 파일:

- `auto_restart_bot.sh`
- `deployment/alphatrader.service`

## 🧪 Backtest Automation

백테스트 리포트는 실거래 히스토리 파일(`database/asset_snapshots.json`, `database/profit_history.json`)을 사용해 자동 생성됩니다.

생성 항목:

- 누적 수익률, 최대 낙폭(MDD), 일간 변동성, 연환산 Sharpe
- 승/패 일수, 승률
- 총 실현손익, 거래 건수
- 30일/90일 롤링 수익률(데이터가 충분한 경우)

실행 방법:

```bash
python modules/backtest_runner.py
```

출력 파일:

- `database/backtest_reports/backtest_YYYYMMDD_HHMMSS.md`
- `database/backtest_latest.json`

### 🧪 Unit Tests

매도 안전 로직(`safe_sell`)과 호가단위 헬퍼 단위 테스트:

```bash
python -m unittest tests.test_safe_sell -v
```

외부 KIS API를 mocking 하므로 토큰 없이 실행됩니다.

## 🖥️ Dashboard

대시보드에서 다음을 확인할 수 있습니다.

- 총 자산, 손익, 수익률
- 최근 주문 상태 및 로그
- 미국/국내 보유 포지션 요약
- 운영 상태(봇 실행 여부, 시장 상태, 최신 업데이트)
- 오늘의 의사결정 요약(1문장 브리프)

관련 코드:

- `web/app.py`
- `web/templates/dashboard_v2.html`
- `web/static/dashboard.js`
- `web/static/dashboard.css`

## 📁 Project Structure

```text
modules/      브로커 API, 전략 보조, 알림, 분석 모듈
strategies/   기술적 분석/변동성 돌파 전략
web/          FastAPI 대시보드
database/     캐시/로그/스냅샷/전략 히스토리
deployment/   서비스/배포 스크립트
docs/         운영/설정/기획 문서
```

## 📚 Documentation

- `docs/주식 자동 매매 기획.md`
- `docs/ORACLE_CLOUD_DEPLOY.md`
- `docs/TELEGRAM_SETUP.md`
- `docs/클라우드터널링.md`
- `docs/BACKTEST_AUTOMATION.md`

## 🛣️ Roadmap

- 주문/체결/실패 이벤트의 대시보드 가시성 강화
- 전략별 백테스트 리포트 자동화
- 모바일 대시보드 접근성 개선
- 알림 채널/필터 세분화

## 🛠️ 핫픽스 노트

- **2026-05-27 — 실시간 미국 주식 Websocket 하이브리드 스트리밍 엔진 출시 (v2.6)**
  - **기능 도입:** KIS 미국 실시간 시세 프로토콜(`HDFSZC413000` / `H0STCNT0`)을 연동한 백그라운드 실시간 가격 모듈(`modules/kis_websocket.py`) 설계 및 탑재.
  - **초저지연 돌파 감시:** 기존 1초~5초 간격의 수동 REST API 가격 폴링 대신, 백그라운드 웹소켓 시세 스트리밍을 실시간 수신하여 캐시(`WS_PRICES`)에 보관하고 판단에 최우선적으로 활용.
  - **성능 및 레이트 리밋 제어 최적화:** 실시간 가격이 변동성 돌파 타겟 미만일 경우 불필요한 REST API 요청을 100% 차단하여 증권사 레이트 리밋 소모를 원천 방어.
  - **이중 검증 안전성 (Double-Check Buy):** 웹소켓 시세가 타겟을 돌파한 순간에만 REST API `get_quote`를 실시간 호출하여 최종 체결 상태 및 실시간 누적 거래량 급증(`tvol`) 여부를 이중 크로스 체크 후 안전하게 전수 주문.
  - **철저한 예외 및 거래소 접두사 보정:** 미국 거래소(NASDAQ - `DNAS`, AMEX - `DAMS`, NYSE - `DNYS`)별 전송 코드 구성에 맞춰 자동 매핑 및 세션 연결 지원. 세션 단절 시 REST API로의 무중단 실시간 Seamless Fallback(물 흐르듯 자동 전환) 설계.


- **2026-05-26 — 미국장 거래 중단 이슈 수정**
  - 증상: 수 주간 US 시장에서 단 한 건도 매매가 발생하지 않음.
  - 원인: `PortfolioManager.generate_and_save_portfolio()` 가 `TARGET_TICKERS_US_1X/3X` 를 `{symbol, weight}` 형태로만 저장했고, `run_bot.py` 의 메인 루프가 `t_obj['exchange']` 로 직접 접근하면서 `KeyError: 'exchange'` 가 발생 → `job()` 의 US 세션이 매 사이클 즉시 크래시.
  - 수정:
    - `run_bot.py` 에 `US_EXCHANGE_MAP` / `_resolve_us_exchange()` 추가 → 동적 포트폴리오 로드 시 누락된 `exchange` 자동 보정.
    - 매매 루프(`for t_obj in tickers`)는 `t_obj.get('exchange')` 로 안전 접근하고, 없으면 심볼 기준으로 거래소 매핑 적용.
    - `modules/portfolio_manager.py` 가 이제 US 종목 저장 시 `exchange` 필드를 포함.
    - 기존 `database/portfolio_target.json` 도 거래소 필드를 백필.

## ⚠️ Disclaimer

이 프로젝트는 정보 제공 및 개인 자동화 목적입니다.
투자 판단과 손익의 책임은 사용자에게 있으며, 실거래 전 반드시 모의투자 환경에서 충분히 검증하세요.
