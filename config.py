import os
from dotenv import load_dotenv

load_dotenv()

# [v8.0] 증권사 = 토스증권 Open API (한국투자증권 API 는 제거됨).
# EXECUTION_MODE: shadow = 판단·로그·알림은 전부 돌지만 주문은 전송하지 않는다 (기본).
#                 live   = 실제 주문. 토스는 공식 모의투자 서버가 없으므로 paper 는 없다.
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "shadow").strip().lower()

TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET")
TOSS_ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ")   # 비우면 종합매매 계좌가 하나일 때 자동 선택
TOSS_BASE_URL = os.getenv("TOSS_BASE_URL", "https://openapi.tossinvest.com")

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
