"""
Grok (xAI) AI 분석기
- xAI의 Grok 모델을 사용한 시장 감성 분석
- X(트위터) 기반 실시간 데이터에 강점
"""
import requests
import json
from config import GROK_API_KEY


class GrokAnalyst:
    def __init__(self):
        self.api_key = GROK_API_KEY
        self.base_url = "https://api.x.ai/v1"
        self.model = "grok-3-mini"  # 무료 티어 모델
        
        if not self.api_key or self.api_key.strip() == "":
            print("[Grok] API Key is missing. Grok analysis will be skipped.")
            self.available = False
        else:
            self.available = True

    def check_market_sentiment(self, news_text, persona="aggressive"):
        """Grok을 사용한 시장 감성 분석"""
        if not self.available:
            return None  # None = 이 LLM은 사용 불가
        
        if not news_text:
            return {"risk_level": "LOW", "can_buy": True, "reason": "No news, skipping.", "source": "grok"}

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
            
            # Clean markdown
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            
            parsed = json.loads(text.strip())
            parsed["source"] = "grok"
            return parsed
            
        except Exception as e:
            print(f"[Grok] Analysis failed: {e}")
            return None
