import os
from dotenv import load_dotenv

load_dotenv()

# Broker selection. 새 장중 전략은 검증 완료 전까지 shadow 주문만 허용한다.
# 1단계에서는 기존 KIS 매매를 유지하고 Toss는 shadow 데이터만 수집한다.
# run_bot의 주문 경로가 정규 브로커 모델로 이관되기 전 BROKER=toss로 표시하면
# 실제 실행 브로커와 계기판이 달라지므로 기본값을 정직하게 KIS로 둔다.
BROKER = os.getenv("BROKER", "kis").strip().lower()
MARKET_DATA_BROKER = os.getenv("MARKET_DATA_BROKER", "toss").strip().lower()
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "shadow").strip().lower()

# Toss Securities Open API
TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET")
TOSS_ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ")
TOSS_BASE_URL = os.getenv("TOSS_BASE_URL", "https://openapi.tossinvest.com")

# KIS API Config — 단계적 전환/롤백 기간에만 유지
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_CANO = os.getenv("KIS_CANO")
KIS_ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD")
KIS_MOCK = os.getenv("KIS_MOCK", "False").lower() == "true"

# Gemini API Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Multi-LLM API Keys
GROK_API_KEY = os.getenv("GROK_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Google OAuth Config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Dashboard Authentication
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY")

# Base URLs
KIS_URL_REAL = "https://openapi.koreainvestment.com:9443"
KIS_URL_MOCK = "https://openapivts.koreainvestment.com:29443"
KIS_BASE_URL = KIS_URL_MOCK if KIS_MOCK else KIS_URL_REAL
