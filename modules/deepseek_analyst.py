"""
DeepSeek AI 분석기
- DeepSeek 모델을 사용한 시장 감성 분석
- 비용 효율적, 아시아 시장 관점에 강점
"""
import requests
import json
from config import DEEPSEEK_API_KEY


class DeepSeekAnalyst:
    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY
        self.base_url = "https://api.deepseek.com"
        self.model = "deepseek-chat"
        
        if not self.api_key or self.api_key.strip() == "":
            print("[DeepSeek] API Key is missing. DeepSeek analysis will be skipped.")
            self.available = False
        else:
            self.available = True

    def health_check(self):
        """API 연결 상태 확인 (최소 비용 요청)"""
        if not self.available:
            return False
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": self.model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 3
            }
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=data,
                timeout=10
            )
            if response.status_code in (401, 402, 403):
                error_msg = response.text[:200]
                print(f"[DeepSeek] Health check FAILED ({response.status_code}): {error_msg}")
                self.available = False
                return False
            response.raise_for_status()
            print("[DeepSeek] Health check PASSED ✓")
            return True
        except Exception as e:
            print(f"[DeepSeek] Health check FAILED: {e}")
            self.available = False
            return False

    def check_market_sentiment(self, news_text, persona="aggressive"):
        """DeepSeek을 사용한 시장 감성 분석"""
        if not self.available:
            return None
        
        if not news_text:
            return {"risk_level": "LOW", "can_buy": True, "reason": "No news, skipping.", "source": "deepseek"}

        persona_instructions = {
            "aggressive": "You are an AGGRESSIVE trader. Only stop buying if there is a CONFIRMED GLOBAL CATASTROPHE. Volatility is opportunity.",
            "neutral": "You are a BALANCED trader. Weigh risks and rewards equally.",
            "conservative": "You are a CONSERVATIVE trader. Capital preservation is priority #1."
        }
        
        selected_instruction = persona_instructions.get(persona, persona_instructions["aggressive"])

        prompt = f"""Act as a stock trading AI assistant.
Persona: {selected_instruction}

Here are the latest news headlines regarding US Tech Market & Fed:
{news_text}

Critical Check:
1. Is there any MAJOR crash signal matching your persona's risk tolerance?
2. Is the sentiment predominantly Fear?

Reply with JSON ONLY (no markdown):
{{
    "risk_level": "HIGH" or "LOW",
    "can_buy": true or false,
    "market_condition": "CRASH" or "BEARISH" or "NEUTRAL" or "BULLISH",
    "reason": "short summary"
}}"""

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200
            }
            
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=data,
                timeout=15
            )
            response.raise_for_status()
            
            result = response.json()
            text = result['choices'][0]['message']['content'].strip()
            
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            
            parsed = json.loads(text.strip())
            parsed["source"] = "deepseek"
            return parsed
            
        except Exception as e:
            print(f"[DeepSeek] Analysis failed: {e}")
            return None
