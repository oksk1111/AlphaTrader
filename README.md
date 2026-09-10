# Alphatrader

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/web-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![KIS API](https://img.shields.io/badge/broker-KIS_API-orange)](#)
[![Telegram](https://img.shields.io/badge/notify-Telegram-2CA5E0)](#)

미국/국내 주식 자동매매 시스템입니다.
**Scored DCA(점수 기반 분할매수)** 전략으로, 추세·뉴스·섹터·낙폭 신호를 0~1 **점수**로 환산해
**매수 여부가 아니라 매수 크기**를 결정합니다. 판단은 하루 한 번이 아니라 장중 5분마다 반복되며,
완전 차단은 자동 해제 조건을 가진 소수의 veto에만 허용합니다.
한국장/미국장에 서로 다른 AI 판단 성향(persona)을 적용하고, FastAPI 대시보드로 운영 상태를 모니터링합니다.

> 🔴 **v6.1 (2026-09-10) — 계기판을 먼저 고쳤습니다**: v6.0 배포 이틀 뒤에도 거래는 0건이었습니다.
> 프로덕션 로그를 보니 미국장이 열려 있던 40분간 봇의 매매 로그가 **한 줄도 없었고**,
> 화면에 뜨던 로그는 전부 대시보드가 자기 계좌 캐시를 갱신하며 쓴 것이었습니다.
> 계기판 세 개가 동시에 거짓말을 하고 있었습니다 — ① 로그 화면이 **봇이 쓰지 않는 파일**을
> 표시(날짜 고정 파일명 + "가장 최근 파일" 추측), ② 시장 상태가 서머타임을 몰라 개장 중에도
> `CLOSED` 표시, ③ `pgrep` 결과만 보고 멈춘 봇을 `🟢 Running` 으로 표시.
> 그 뒤에는 실제 고장이 있었습니다 — US 예수금 $330.64를 12종목으로 나눠 종목당 $27.55가 되고,
> 게다가 `int(1주) x 0.5 = 0주` 로 **모든 매수가 조용히 증발**하고 있었습니다.
>
> **이 프로젝트가 v3→v6까지 네 번 "전면 개편"을 하고도 증상이 남은 이유는 전략이 아니라
> 계기판이 고장나 있었기 때문입니다.** v6.1은 로그 회전·단일 시계·heartbeat 기반 생존 판정·
> `job()` 바깥 감시를 도입하고, 검증 루프에 **캡처율/회전손실** 지표와 **실계좌 사이징 검사**를
> 추가했습니다. 그 지표로 청산 규칙 결함(완만한 상승장 캡처율 **-34%**)을 발견해
> 손절/트레일링을 `-12% / +10% / 6%` 로 재설정했습니다.
>
> 자세한 내용은 `AGENTS.md` 의 v6.1 섹션을 보세요.

> 🟠 **v6.0 전면 개편 (2026-09-08)**: 한국장·미국장 모두 **몇 달간 거래가 0건**이던 사고를
> 근본 수정했습니다. 원인은 5가지가 겹쳐 있었습니다 — ① USD 예수금 0을 정상으로 오인,
> ② 매수 판단이 세션당 종목별 1회뿐(장중 재평가 경로 부재), ③ `가격 > MA20` 추세 게이트가
> DCA의 정의와 모순, ④ 드로다운 서킷브레이커가 해제 조건 없는 **영구 래치**,
> ⑤ 미국 서머타임 미반영. 무엇보다 **어느 단계에서도 예외가 나지 않아 몇 달간 발견되지
> 않았습니다.** v6.0은 이진 게이트를 점수 체계로 대체하고, 연속 무매수를 감지하는
> 자가진단 루프를 추가했습니다.
>
> 배경과 근거는 `AGENTS.md`의 v6.0 섹션과 `docs/주식 자동 매매 기획.md`를 참고하세요.
> 투자 용어가 낯설다면 `docs/baseknowledge.md`부터 읽어보세요.

빠른 이동: [Key Features](#-key-features) · [Quick Start](#-quick-start) · [Configuration](#-configuration) · [Backtest Automation](#-backtest-automation) · [Dashboard](#️-dashboard)

## ✨ Key Features

| 모듈 | 기능 | 설명 |
|------|------|------|
| Trading Engine | 실시간 웹소켓 + 자동매매 | 장 상태 감지 및 미국 시장 실시간 웹소켓(WebSocket) 스트리밍 가격 수신을 통한 초저지연 실시간 시세 감시 및 고성능 변동성 돌파 구현 |
| Strategy | 다중 전략 | `aggressive_dca`(기본, v6.0부터 점수 기반), `day`, `swing`, `dca` 전략과 `safe/risky` 모드 지원 |
| **Scored DCA** | **점수가 매수 크기를 정한다** | 추세/구조/눌림/갭/뉴스/노출을 0~1 점수로 합성해 수량에 곱함. 점수 미달은 차단이 아니라 `defer`(다음 주기 재평가) (v6.0, `modules/decision_engine.py`) |
| **장중 연속 재평가** | **하루 1회 판정 폐기** | 개장 루프와 장중 루프가 동일한 판단 함수를 5분 주기로 호출. 개장 때 조건이 나빴어도 장중에 좋아지면 같은 세션에 매수 (v6.0) |
| **DST 정확 시장 시계** | **서머타임/휴장일 반영** | `zoneinfo` 기반 거래소 현지시각. 하드코딩 KST 시간표 폐기 — 서머타임 기간 개장 첫 1시간 상실 및 폐장 후 오발주 해결 (v6.0, `modules/market_clock.py`) |
| **낙폭 사다리** | **영구 래치 → 자동 해제** | 고점 대비 진짜 낙폭 기준 사이즈 축소(100→60→30→0%). cool-off 3영업일 또는 낙폭 절반 회복 시 자동 해제 (v6.0, `modules/risk_state.py`) |
| **전략 자가진단** | **조용한 정지 감지** | 연속 무매수 세션 추적 → 3세션 경고 / 7세션 긴급. 지배적 차단 사유와 조치 힌트를 함께 통보 (v6.0, `modules/health_monitor.py`) |
| **Sector News Veto** | **섹터 특화 뉴스 차단** | 종목이 속한 섹터(반도체/나스닥테크/2차전지 등) 전용 뉴스를 별도 조회해, 시장 전체는 멀쩡해도 특정 섹터만 붕괴 중이면 해당 섹터 매수만 차단 (v5.0 신규, `modules/sector_news.py`) |
| Market-Specific AI Persona | 시장별 AI 성향 | US=`neutral`, KR=`conservative` 기본 적용 — 국내 개별 이슈 변동성이 더 크다는 전제로 KR을 더 보수적으로 (v5.0) |
| Risk Control | 리스크 관리 | 손절(**-7%**, v6.0 조정), 트레일링 스탑, 갭다운/연속하락 방어, 낙폭 사다리, **시장가→지정가 fallback 매도** |
| Rebound Trigger | 변동성 반등 매수 | 큰 하락 후 당일 반등 시 50% 수량으로 역방향 진입 (v2.3) |
| Partial Take-Profit | 1차 부분 익절 | Trailing 활성가 도달 시 50% 청산 + 잔량 trailing (v2.4) |
| Breakeven Stop | 본전 스탑 | 고점 +3%/+4% 도달 후 손절선을 매수가 +0.2% 위로 끌어올림 (v2.5) |
| Correlation Cap | 상관 그룹 한도 | 동일 섹터 그룹 동시 보유 최대 2종목 (v2.5 도입, v4.0 비활성화 → v5.0 재활성화) |
| Losing Streak Throttle | 일일 손실 회로차단 | 손절 누적 시 신규 매수 일시 중단 (v2.5 도입, v4.0 비활성화 → v5.0 재활성화) |
| ATR Dynamic Stop | 변동성 적응 손절 | 14일 ATR 기반으로 종목별 손절폭 동적 보강 (v2.5) |
| Strategy Verification Loop | 배포 전 전략 검증 | 5개 시장 국면 × N세션 시뮬레이션으로 "돌아는 가는데 아무것도 안 사는" 상태를 CI에서 차단 (v6.0, `scripts/verify_strategy_loop.py`) |
| AI Assist | 시장 보조 분석 | 뉴스 기반 위험도 판단 및 매수 제한(페르소나 반영), v5.0부터 기본 활성화 |
| Dynamic Portfolio | 동적 포트폴리오 | 고품질 ETF 풀 중 모멘텀/안정성이 우수한 종목을 시스템이 주기적으로 자동 필터링 및 교체(삭제) |
| AI Consensus Policy | 설정 기반 합의 | 쿼럼/매수비율/CRASH veto/동률처리를 설정으로 제어 |
| Auto Strategy | 자동 전략 전환 | 시장/자산/포지션 상태 기반 전략/모드/페르소나 자동 최적화 |
| Dashboard | 운영 관제 | 총자산, 수익률, 보유종목, 최근 주문/로그를 웹에서 확인 |
| Dashboard Auth | 접근 통제 | API 키 기반 인증 + Rate Limiting + 쿠키 세션 (v3.1) |
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
DASHBOARD_API_KEY=...   # 대시보드 접근 키 (미설정 시 랜덤 생성)
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

- `strategy`: `aggressive_dca`(기본) | `day` | `swing` | `dca`
- `trading_mode`: `safe` | `risky`
- `persona`: `aggressive` | `neutral` | `conservative`

**`aggressive_dca` (v6.0 — 점수 기반)**

| 키 | 기본값 | 의미 |
|----|--------|------|
| `min_buy_score` | **0.35** | 이 점수 미만은 `defer`(다음 주기 재평가). 낮추면 자주 매수, 높이면 까다롭게 |
| `max_position_pct` | **25.0** | 종목당 총자산 대비 최대 노출(%). 물타기 폭주를 '금지'가 아니라 '한도'로 통제 |
| `reeval_interval_sec` | **300** | 장중 재평가 주기(초) |
| `sentiment_ttl_sec` | **900** | AI 감성 세션 캐시 수명(초). 종목마다 LLM을 호출하지 않기 위함 |
| `panic_gap_down_threshold_pct` | **8.0** | 이 이상 갭다운이면 하드 veto (익일 자동 해제) |
| `sector_news_veto_enabled` | **true** | 섹터 특화 뉴스 CRASH 시 해당 섹터만 차단 |

> ⚠️ v5.x의 `skip_ai_check` / `skip_trend_filter` / `skip_correlation_check` /
> `portfolio_drawdown_halt_pct` / `instant_buy_on_open` 는 **제거**되었습니다.
> 이진 게이트 자체가 폐기되어 의미가 없습니다.

**`risk_management`**

- `stop_loss_pct` (**-7.0%**, v6.0에서 -4.0%→-7.0% 조정 — 근거는 아래 검증 루프)
- `trailing_stop_activation_pct` (**5.0%**), `trailing_stop_drop_pct` (**3.5%**)
- `gap_down_threshold_pct` (**5.0%**), `consecutive_decline_pct` (**5.0%**)
- `rebound_buy_enabled`, `rebound_drop_threshold_pct` (**3.0%**), `rebound_intraday_bounce_pct` (**1.0%**)
- `partial_tp_enabled` (**true**), `partial_tp_ratio` (**0.5**)
- `breakeven_enabled` (**true**), `breakeven_trigger_pct_us` (**2.5**), `breakeven_buffer_pct` (**0.2**)
- `correlation_cap_enabled` (**true**), `correlation_max_per_group` (**2**)
- `losing_streak_enabled` (**true**), `losing_streak_max_stops` (**3**), `losing_streak_daily_pnl_pct` (**-3.0**)
- `atr_dynamic_stop_enabled` (**true**), `atr_period` (**14**), `atr_stop_multiplier` (**1.8**)

> `portfolio_drawdown_pct` 는 **제거**되었습니다. 이 값은 '고점 대비 낙폭'이 아니라
> '원가 대비 평가손실'을 재던 것이라, 계좌가 원가 대비 7% 물리면 해제 조건 없이
> 신규 매수가 영구 차단되는 래치였습니다. `modules/risk_state.py` 의 낙폭 사다리로 대체.

**낙폭 사다리** (`modules/risk_state.py`, 설정 불필요)

| 고점 대비 낙폭 | 신규 매수 사이즈 | 해제 |
|----------------|------------------|------|
| 0~7% | 100% | — |
| 7~12% | 60% | 낙폭 회복 시 자동 |
| 12~18% | 30% | 낙폭 회복 시 자동 |
| 18%+ | 0% | cool-off 3영업일 **또는** 낙폭 절반 회복 |

- `dca_settings`: 일간 투자비중/매수상한/세션 매수 횟수
  - `daily_investment_pct` (기본 **30%**), `max_investment_usd` (**$2,000**), `max_buys_per_session` (**10**)
- `market_settings`: 시장별 override. 기본값 `us.persona=neutral`, `kr.persona=conservative`.

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

**세션 트리거 (v6.0)**

| 시장 | 개장 (거래소 현지) | KST 환산 |
|------|--------------------|----------|
| 🇰🇷 KR | 09:00~15:20 KST | 09:00~15:20 |
| 🇺🇸 US 서머타임 (3월 둘째 일요일~11월 첫째 일요일) | 09:30~16:00 EDT | **22:30~05:00** |
| 🇺🇸 US 표준시 | 09:30~16:00 EST | **23:30~06:00** |

> v5.x는 미국장을 KST 23:30~06:00으로 하드코딩해, 1년의 약 2/3인 서머타임 기간에
> 개장 첫 1시간을 놓치고 폐장 후 1시간 동안 닫힌 시장에 매도 주문을 냈습니다.
> v6.0은 `zoneinfo`로 거래소 현지시각을 계산하며 휴장일/조기폐장도 반영합니다.
>
> 또한 v5.x의 **5분짜리 트리거 창**(KR 09:00~09:05, US 23:30~23:35)을 폐기했습니다.
> 그 5분 안에 프로세스가 살아있지 못하면(배포·재부팅·네트워크 순단) 그날 거래가
> 통째로 사라졌습니다. 이제 **장이 열려 있고 해당 세션을 아직 안 돌렸으면 진입**합니다.

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

### 🧪 Tests & Strategy Verification

```bash
# 전체 테스트 (외부 API는 mocking — 토큰 없이 실행 가능)
pytest tests/ -q

# v6.0 회귀 테스트 — 5대 사고 원인에 1:1 대응
pytest tests/test_v60_strategy.py -v      # DST / 점수 판단 / 낙폭 사다리 / 자가진단
pytest tests/test_v60_integration.py -v   # job() 배선 (장중 재평가 경로 존재 여부 등)

# v6.1 회귀 테스트 — 관측 계층 + 소액계좌 사이징
pytest tests/test_v61_observability.py -v # 로그 회전 / 단일 시계 / 좀비 판정 / 1주 바닥

# 전략 검증 루프 — "돌아는 가는데 아무것도 안 사는" 상태를 배포 전에 차단
# 시드 5개 평균으로 판정합니다 (단일 시드 통과는 운일 수 있음)
python scripts/verify_strategy_loop.py --sessions 80 --seed 42 --seeds 5 --quiet
python scripts/verify_strategy_loop.py --sessions 80 --seed 7  --seeds 5 --quiet

# 청산 파라미터 스윕 (손절/트레일링을 바꿀 때)
python scripts/verify_strategy_loop.py --seeds 5 --quiet --stop -12 --trail-act 10 --trail-drop 6
```

`verify_strategy_loop.py` 는 두 층으로 검증합니다.

**[0] 소액 계좌 사이징** — 실계좌 수치($330.64/12종목, 2,715,678원/21종목)를 그대로 넣어
매수 수량이 0으로 죽지 않는지 봅니다. 국면 시뮬레이션은 자금 '규모'를 모델링하지 않으므로
이 결함을 **원리적으로** 잡을 수 없습니다 — v6.0이 전 국면 통과 상태로 배포되고도
실계좌에서 한 주도 못 산 이유가 이것입니다.

**[1] 국면 시뮬레이션** — 5개 시장 국면(강한 상승/완만한 상승/횡보/조정/폭락)을 합성
가격으로 만들고 장중 재평가·손절·트레일링까지 시뮬레이션합니다. 상승장에서 자본이 놀면
(v5.x 회귀) 실패, 폭락장에서 과다 노출이면(v4.0 회귀) 실패, 그리고 v6.1부터는
**캡처율/회전손실**이 기준 미달이어도 실패합니다.

> **캡처율 = 수익률 / (평균노출 x B&H수익)** — 트레일링이 승자를 조기에 털어내는가
> **회전손실 = 수익률 - (평균노출 x B&H수익)** — 손절 휩쏘로 그냥 보유보다 더 잃는가
>
> v6.0은 노출도만 봤습니다. 그래서 전 국면 노출도 22~25%로 통과하면서도 완만한 상승장
> 캡처율이 **-34%**(오른 만큼을 전부 반납)인 것을 보지 못했습니다. **노출도는 "얼마나
> 실려 있나"만 알려주고 "실려 있는 동안 얼마나 지켰나"는 알려주지 않습니다.**

기준선 (80세션 x 5시드, 손절 -12% / 트레일링 활성 +10% / 하락폭 6%):

| 국면 | 노출도 | 매수 | 매도 | 전략 | 동일노출 보유 | 캡처율 | 회전손실 | Buy&Hold |
|------|--------|------|------|------|---------------|--------|----------|----------|
| 강한 상승장 | 26% | 40 | 2 | +5.4% | +7.5% | 72% | -2.1%p | +29.2% |
| 완만한 상승 | 27% | 25 | 1 | +1.4% | +1.9% | 76% | -0.4%p | +7.0% |
| 횡보장 | 26% | 42 | 1 | -1.9% | -0.8% | — | -1.2%p | -3.0% |
| 조정장 | 25% | 52 | 2 | -7.4% | -5.3% | — | -2.1%p | -21.1% |
| 폭락장 | 20% | 142 | 6 | **-17.7%** | -10.8% | — | -7.0%p | **-54.3%** |

> **알려진 한계**: 단일 종목 시뮬이라 종목 노출 한도(25%)가 구속력을 갖습니다(실운용은
> 12~21종목). ATR 동적 손절·레버리지 스케일링·슬리피지·수수료는 모델링하지 않으므로
> 회전손실 수치는 **낙관적인 하한**입니다. 합성 GBM 데이터라 갭·실적·팻테일도 없습니다.
> **절대 수익률이 아니라 국면 간 상대 관계와 캡처율/회전손실**을 보세요.
> 멀티 티커 포트폴리오 모델이 다음 과제입니다.

## 🖥️ Dashboard

대시보드에서 다음을 확인할 수 있습니다.

- 총 자산, 손익, 수익률
- 최근 주문 상태 및 로그
- 미국/국내 보유 포지션 요약
- 운영 상태(봇 생존 여부, 시장 상태, 최신 업데이트)
- 오늘의 의사결정 요약(1문장 브리프)

> **[v6.1] 봇 상태 표시가 바뀌었습니다.** 이전에는 `pgrep` 결과만 보고 두 가지 상태만
> 표시했는데, **멈춘 프로세스도 pgrep에는 잡힙니다.**
> 이제 `database/heartbeat.json` 의 나이로 판정합니다:
>
> | 표시 | 의미 |
> |------|------|
> | `🟢 Running` | heartbeat 가 5분 이내 — 정상 |
> | `🟠 Stalled` | 프로세스는 있으나 5분 넘게 진행 없음 → 감시 스크립트가 강제 재기동 |
> | `🟡 Running (heartbeat 미확인)` | 구버전 봇이 돌고 있음 (재배포 필요) |
> | `🔴 Stopped` | 프로세스 없음 |
>
> 로그 화면도 이제 `database/trading.log`(봇 로그)를 **직접** 읽습니다. 각 줄에는
> `[bot]` / `[dash]` 태그가 붙어 어느 프로세스가 쓴 것인지 구분됩니다.

관련 코드:

- `web/app.py`
- `web/templates/dashboard_v2.html`
- `web/static/dashboard.js`
- `web/static/dashboard.css`

## 📁 Project Structure

```text
modules/
  market_clock.py      DST/휴장일 정확 시장 시계 (v6.0)
  decision_engine.py   점수 기반 매수 판단 (v6.0)
  risk_state.py        고점 대비 낙폭 사다리 + 자동 해제 (v6.0)
  health_monitor.py    연속 무매수 자가진단 (v6.0)
  kis_api.py / kis_domestic.py / kis_websocket.py   브로커 API
  multi_llm.py / sector_news.py / *_analyst.py      AI 분석
  portfolio_manager.py / backtest_runner.py / opro_optimizer.py
strategies/   기술적 분석/변동성 돌파 전략
scripts/
  verify_strategy_loop.py   전략 검증 루프 (v6.0)
web/          FastAPI 대시보드
database/     캐시/로그/스냅샷/리스크 상태/전략 건강 기록
deployment/   서비스/배포 스크립트
docs/         운영/설정/기획 문서
```

## 📚 Documentation

- `docs/주식 자동 매매 기획.md` — 전략 기획 및 버전 이력
- `docs/baseknowledge.md` — **[v5.0 신규]** 퀀트투자 입문자를 위한 용어/개념 설명
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

- **2026-09-10 — v6.1: 계기판이 거짓말을 하고 있었다 (관측 계층 전면 수정)**
  - **증상**: v6.0 배포(9/8) 이틀 뒤에도 거래 0건. 대시보드는 봇을 정상 실행 중으로,
    시장 상태를 `CLOSED` 로 표시했고, 로그에는 계좌 캐시 갱신만 6분 간격으로 반복.
  - **결정적 단서**: 22:34~23:14 KST(미국 서머타임 정규장 중)의 로그에 봇의
    1분 주기 heartbeat 줄이 **하나도 없었다.** 화면의 로그는 전부 대시보드가
    5분마다 자기 계좌 캐시를 갱신하며 쓴 것이었다(`CACHE_MAX_AGE_SECONDS=300`).
  - **관측 계층 결함 3가지**:
    1. `logging.FileHandler(f"trading_{today}.log")` — 프로세스 시작 시 파일명이
       확정되고 **날짜 회전이 없다.** 9/8에 뜬 봇은 계속 `trading_20260908.log`에
       쓰고, 나중에 재시작된 대시보드가 `trading_20260910.log`를 새로 만든다.
       그런데 대시보드는 `sorted(glob("trading_*.log"))[-1]`로 "가장 최근 날짜"를
       추측 → **자기 로그 파일**을 골랐다.
    2. `web/app.py:get_market_status()`가 US를 `23:30~06:00 KST`로 하드코딩.
       v6.0은 `run_bot`의 시계만 `market_clock`으로 옮기고 이 함수를 놓쳤다.
    3. 봇 생존을 `pgrep`으로만 판정. 44분간 멈춰 있어도 "실행 중"으로 표시.
  - **실제 고장 3가지**:
    4. **소액 계좌에서 매수 수량이 구조적으로 0.** US 예수금 $330.64를 후보
       12종목으로 나눠 종목당 $27.55(1주 미만). 게다가 호출부가
       `int(qty) * size_multiplier` — 정수화 **뒤에** 배율을 곱해 `int(1x0.5)=0`.
       배율이 1.0이 아닌 모든 매수가 조용히 증발했다.
    5. `auto_restart_bot.sh:is_market_hours()`도 US를 `23:20~06:10 KST`로 하드코딩.
       게다가 `check_and_restart_bot`이 그 창 **안에서만** 호출되어, 서머타임 기간
       개장 첫 50분간 봇이 죽어 있어도 아무도 되살리지 않았다.
    6. 자가진단(`health_monitor`)이 `job()` **안에서** 세션 종료 시 돌기 때문에,
       `job()` 자체가 안 도는 최악의 고장이 정확히 사각지대였다.
  - **조치**:
    - `TimedRotatingFileHandler`(자정 회전) + 프로세스별 파일 분리
      (봇=`trading.log`, 대시보드=`dashboard.log`) + 줄마다 `[bot]`/`[dash]` 태그
    - 대시보드가 `modules.logger.BOT_LOG_FILE`을 **직접** 읽음 (추측 금지)
    - `web/app.py`·`auto_restart_bot.sh` 모두 `market_clock`으로 위임 (시계 단일화)
    - `database/heartbeat.json` 도입 — 감시/화면이 파일의 **나이**로 생존 판정.
      장중 5분 이상 정지 시 좀비로 보고 강제 재기동, 화면은 `Stalled` 표시
    - 감시 스크립트가 봇을 **장 안팎 없이 항상** 살려둠 (개장 순간 프로세스 부재 방지)
    - `_check_session_stall()` — 메인 루프에서 "개장 10분 경과, `job()` 미진입" 감시
    - 사이즈 배율을 **금액에** 곱하도록 수정, 자금은 후보 수가 아니라 **채울 수 있는
      슬롯 수**로 분할, `buy` 판정 시 1주 바닥 보장
    - 검증 루프에 **캡처율/회전손실** 지표 + **실계좌 사이징 검사** + 시드 5개 평균 판정
  - **전략 변경**: 위 지표로 청산 규칙 결함 발견 — v6.0의 `-7%/+5%/3.5%`는 여전히
    일간 변동성(1.1~3.5%) 안이라 **전 국면에서 "같은 노출로 그냥 보유"보다 손해**
    였다(완만한 상승장 캡처율 **-34%**). 스윕 후 `-12%/+10%/6%` 채택
    (캡처율 72%/76%, 휩쏘 매도 절반 이하). `atr_stop_max_pct`도 -12로 함께 확장하지
    않으면 손절폭이 -10으로 되감기므로 같이 조정.
  - **감시 장치를 넣으며 만든 자책골 2개 (실서버 관측으로 잡음)**:
    - `job()` 은 세션 내내 메인 루프를 점유하므로 heartbeat 가 멈춘다 →
      **좀비 감시가 정상 매매 중인 봇을 죽일 뻔했다.** `job()` 안에서 5분 이상
      침묵할 수 있는 구간을 전부 덮음:
      `job-enter → session-prep → scan → sentiment → evaluate → watchloop`
    - `deploy.sh`(pkill 후 3초 뒤 재기동)와 항상 도는 감시가 겹치면 **봇이 두 개**
      돌아 같은 신호에 주문이 두 번 나간다 → `database/.deploying` 배포 락
      (`trap EXIT` 해제, 5분 초과 시 무시) + `kill_duplicate_bots()`(경과시간
      기준으로 가장 오래된 하나만 유지) 추가
  - **`/api/status` 확장**: `session_active`, `heartbeat_source` 노출.
    "봇이 살아 있다"와 "매매 세션을 돌고 있다"는 다른 상태인데 구분할 값이 없었다.
  - **배포 후 실서버 확인** (2026-09-10 23:5x KST, 미국장 개장 중):
    `{"market_status":"US","heartbeat_source":"watchloop","session_active":true}`
    → 서머타임 반영(수정 전 `CLOSED`), 매매 세션 정상 진행 확인
  - **파일**: `modules/logger.py`, `web/app.py`, `auto_restart_bot.sh`, `run_bot.py`,
    `deployment/deploy.sh`, `user_config.json`, `scripts/verify_strategy_loop.py`,
    `tests/test_v61_observability.py`, `.github/workflows/deploy.yml`

- **2026-09-08 — v6.0 전면 개편: 몇 달간의 거래 정지 근본 수정**
  - **증상**: 한국장·미국장 모두 몇 달간 매수 체결 0건. 봇은 매일 정상 부팅하고
    하트비트를 찍었으며 어떤 단계에서도 예외가 발생하지 않았음.
  - **원인 5가지 (전부 서로를 가려주고 있었음)**:
    1. `get_foreign_balance()` 가 USD 통화 라인이 없을 때 `{'deposit': 0}` 을 반환.
       v5.0이 넣은 방어코드는 `'deposit' in foreign_bal` 로 판정 → 키가 **존재**하므로
       정상 분기를 타서 `available_cash=0` 확정. 이 상황을 잡으려던 알림이 정확히
       이 경로를 비껴감. 통합증거금(원화주문) 계좌는 **정상적으로** USD 예수금이 0.
    2. `aggressive_dca` 의 매수 판단이 **세션당 종목별 1회**, 개장 직후에만 실행.
       장중 재평가 경로가 코드에 아예 없었음(`dca` 모드에만 존재).
    3. 추세 게이트 `현재가 > MA20`(KR은 `AND > MA5`)이 DCA의 정의와 모순 —
       하락 구간에 사는 것이 DCA인데 하락 구간 매수를 전부 금지.
    4. 드로다운 서킷브레이커가 '고점 대비 낙폭'이 아니라 '원가 대비 평가손실'을
       재고 있었고 **해제 조건이 없었음**. 자기강화적(안 사니 원가 불변 → 계속 차단).
    5. 미국장 시간을 KST 23:30~06:00으로 하드코딩(EST 기준) → 서머타임 기간
       개장 첫 1시간 상실 + 폐장 후 1시간 오발주. 휴장일 개념 없음.
  - **조치**: 이진 게이트를 점수 체계로 대체(`decision_engine`), 장중 5분 주기
    연속 재평가, `zoneinfo` 기반 시장 시계(`market_clock`), 고점 대비 낙폭 사다리
    + 자동 해제(`risk_state`), 연속 무매수 자가진단(`health_monitor`),
    전략 검증 루프(`scripts/verify_strategy_loop.py`).
  - **부수 수정**: `load_config()` 인코딩 미지정(플랫폼 기본 의존 → UTF-8 고정),
    `num_active_targets` 가 항상 0이라 한 종목에 현금 전액을 배정하던 사이징 버그,
    KST 자정에 진행 중인 US 세션 플래그가 리셋되던 문제.
  - **검증**: 테스트 110개 통과 + 5국면 검증 루프 통과.

- **2026-07-28 — v5.0 전면 개편: 뉴스 게이트 복원 + 섹터 특화 veto 신규 도입**
  - **문제**: v4.0(`aggressive_dca`)이 `skip_ai_check`/`skip_trend_filter`를 전부 켜서
    "게이트 없는 무조건 매수"를 실행하다가, 2026-07 중국발 반도체 수출 규제 뉴스로
    반도체 업종 전체가 붕괴하는 와중에도 이를 전혀 인지하지 못하고 SOXL/SMH를
    계속 물타기 매수 → 몇 주간 큰 손실. 또한 "미국장 거래가 몇 주째 발생하지 않는다"는
    별도 증상도 함께 보고됨 — US 잔고 조회(`get_foreign_balance`) 실패가 알림 없이
    조용히 삼켜져(`available_cash=0` 고정) 세션 내내 "자금 부족"으로 매수가 전부
    차단됐을 가능성이 유력한 원인으로 파악됨(과거 2026-05/06 유사 사고 패턴과 동일).
  - **해결**:
    1. `aggressive_dca`의 `skip_ai_check`/`skip_trend_filter`/`skip_correlation_check`
       기본값을 전부 원복(게이트 ON). "공격적"은 매수 조건 없음이 아니라 매수 주기가
       잦다는 뜻으로 재정의.
    2. **[신규] 섹터 특화 뉴스 veto** (`modules/sector_news.py`, `GeminiAnalyst.check_sector_crash`,
       `MultiLLMAnalyst.check_sector_sentiment`) — 종목이 속한 섹터(반도체/나스닥테크/
       2차전지 등) 전용 뉴스를 Google News RSS로 조회해, 시장 전체가 아니라 해당
       섹터만 국한하여 크래시를 판별. 반도체만 나쁠 때 반도체 종목만 차단하고 다른
       매수 기회는 유지. 페르소나(aggressive 등)와 무관하게 항상 엄격하게 판단.
    3. 포트폴리오 드로다운 서킷브레이커가 **실제로 신규 매수를 차단**하도록 수정
       (기존에는 알림만 보내고 매수는 계속 진행되는 버그).
    4. US 잔고 조회 실패/응답 이상 시 Telegram 알림 추가 (기존에는 로그만 남고 조용히 넘어감).
    5. **세션 매수 0건 워치독**: 후보 종목이 있는데 세션 내내 매수가 한 건도 없으면
       차단 사유와 함께 Telegram 경고.
    6. 시장별 AI persona 차등 적용 (US=`neutral`, KR=`conservative`).
    7. `correlation_cap_enabled`/`losing_streak_enabled`/`llm_consensus.crash_veto` 등
       v4.0에서 defang된 안전장치 전부 재활성화.
  - **테스트**: `tests/test_v50_strategy.py` 신규 10건 (섹터 매핑, 게이트 기본값, 섹터
    캐시/페일세이프) + 기존 66개 전체 통과.

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
