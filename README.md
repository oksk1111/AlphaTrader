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
| Trading Engine | 자동매매 실행 | 장 상태 감지(US/KR) 후 전략별 매매 루프 실행 |
| Strategy | 다중 전략 | `day`, `swing`, `dca` 전략과 `safe/risky` 모드 지원 |
| Risk Control | 리스크 관리 | 손절, 트레일링 스탑, 갭다운/연속하락/포트폴리오 드로다운 방어 |
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
- `risk_management`: 손절/트레일링/드로다운 임계값

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

## ⚠️ Disclaimer

이 프로젝트는 정보 제공 및 개인 자동화 목적입니다.
투자 판단과 손익의 책임은 사용자에게 있으며, 실거래 전 반드시 모의투자 환경에서 충분히 검증하세요.
