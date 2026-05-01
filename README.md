# Alpha Trader 🎯

미국/한국 주식 자동매매 봇 (AI + Hybrid 기술적 분석)

## 📊 프로젝트 개요

변동성 돌파 전략과 AI(Gemini 1.5)를 결합하여 안정적인 수익을 추구하는 자동매매 시스템입니다.
ETF 거래가 힘든 소액 자산가(<1000만원)를 위한 **우량주(Blue Chip) 모드**를 지원하며, 투자 성향(Persona)에 따라 AI의 판단 기준을 조절할 수 있습니다.

## 🎯 투자 전략

1. **Hybrid Strategy**: 
    - **Trader (Rule-based)**: 변동성 돌파(VBO) + 20일 이동평균선(MA20)으로 타점 포착
    - **Analyst (AI-based)**: Gemini 1.5가 거시경제 뉴스를 분석하여 "잠재적 폭락" 위험 필터링
2. **Dynamic Persona**: 사용자의 성향에 따라 AI의 거부권(Veto) 권한을 조정
    - **Aggressive**: 사소한 악재 무시, 모멘텀 중시
    - **Neutral**: 균형 잡힌 리스크 관리
    - **Conservative**: 아주 작은 위험 신호에도 매수 중단
3. **Adaptive Targets**:
    - **Safe Mode (<1000만원)**: 우량주 중심 (NVDA, AAPL, 삼성전자, SK하이닉스 등)
    - **Risky Mode (>=1000만원)**: 2x/3x 레버리지 ETF (TQQQ, SOXL, KODEX 레버리지)

## 🛠️ 기술 스택

- **Broker**: 한국투자증권 (KIS) Open API
- **AI Engine**: Google Gemini 1.5 Flash (비용 효율성 최적화)
- **Language**: Python 3.10+
- **Database**: JSON File System (Local Cache)

## 📋 환경 설정 (`config.py` 또는 `user_config.json`)

`user_config.json` 예시:
```json
{
    "trading_mode": "safe", 
    "strategy": "day",
    "persona": "aggressive",
    "dca_settings": {
        "enabled": true,
        "daily_investment_pct": 5
    }
}
```

- **trading_mode**: `safe` (우량주), `risky` (레버리지)
- **persona**: `aggressive`, `neutral`, `conservative`

## 🚀 실행 방법

### 설치
```bash
pip install -r requirements.txt
```

### 환경변수 (.env)
```env
KIS_APP_KEY=...
KIS_APP_SECRET=...
GEMINI_API_KEY=...
```

### 실행
```bash
# 메인 봇 실행
python run_bot.py
```

### 📊 대시보드
```bash
python web/app.py 
# 또는
streamlit run dashboard.py
```

## ⚠️ 주의사항

1. **투자 책임**: 모든 투자의 책임은 사용자에게 있습니다.
2. **모의투자 권장**: 코드를 수정하거나 전략을 바꿀 때는 반드시 모의투자 계좌(`VTTC...`)로 테스트하세요.
