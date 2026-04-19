import google.generativeai as genai
import requests
import xml.etree.ElementTree as ET
import json
from config import GEMINI_API_KEY

class GeminiAnalyst:
    def __init__(self):
        if not GEMINI_API_KEY or "INSERT" in GEMINI_API_KEY:
            print("[Gemini] API Key is missing. AI analysis will be skipped (Defaulting to Neutral/Positive).")
            self.model = None
            self.available = False
        else:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel('gemini-1.5-flash')
            self.available = True

    def fetch_news(self):
        """CNBC Finance RSS Feed Fetch"""
        url = "https://www.cnbc.com/id/10000664/device/rss/rss.html" # Finance
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            
            headlines = []
            for item in root.findall('./channel/item'):
                title = item.find('title').text
                description = item.find('description').text
                headlines.append(f"- {title}: {description}")
                if len(headlines) >= 10: # Top 10 only
                    break
            
            return "\n".join(headlines)
        except Exception as e:
            print(f"[Gemini] Failed to fetch news: {e}")
            return ""

    def check_market_sentiment(self, news_text, persona="aggressive"):
        if not self.model:
            return {"risk_level": "LOW", "can_buy": True, "market_condition": "NEUTRAL", "reason": "API Key missing, skipping AI check.", "source": "gemini"}
        
        if not news_text:
            return {"risk_level": "LOW", "can_buy": True, "market_condition": "NEUTRAL", "reason": "No news found, skipping AI check.", "source": "gemini"}

        # Define Persona Prompts
        persona_instructions = {
            "aggressive": "You are an AGGRESSIVE trader. You ignore minor fears and focus on momentum. Only stop buying if there is a CONFIRMED GLOBAL CATASTROPHE (Nuclear War, Great Depression). Volatility is opportunity.",
            "neutral": "You are a BALANCED trader. Weigh risks and rewards equally. Avoid buying during clear downtrends or major bad news, but don't panic over small corrections.",
            "conservative": "You are a CONSERVATIVE trader. Preservation of capital is priority #1. If there is ANY hint of instability, rate hikes, or uncertainty, recommend HOLD or SELL. Do not buy unless the market is perfectly calm."
        }
        
        selected_instruction = persona_instructions.get(persona, persona_instructions["aggressive"])

        prompt = f"""
        Act as a stock trading AI assistant.
        Persona: {selected_instruction}

        Here are the latest news headlines regarding US Tech Market & Fed:
        {news_text}

        Critical Check:
        1. Is there any MAJOR crash signal matching your persona's risk tolerance?
        2. Is the sentiment predominantly Fear?

        Reply with JSON ONLY:
        {{
            "risk_level": "HIGH" or "LOW",
            "can_buy": boolean,
            "market_condition": "CRASH" or "BEARISH" or "NEUTRAL" or "BULLISH",
            "reason": "short summary"
        }}
        """
        
        try:
            response = self.model.generate_content(prompt)
            text = response.text.strip()
            # Clean up markdown code blocks if present
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            
            result = json.loads(text)
            # 일관성을 위해 source/market_condition 보장
            result["source"] = "gemini"
            if "market_condition" not in result:
                result["market_condition"] = "BEARISH" if result.get("risk_level") == "HIGH" else "NEUTRAL"
            return result
        except Exception as e:
            print(f"[Gemini] AI Analysis failed: {e}")
            return {"risk_level": "UNKNOWN", "can_buy": False, "market_condition": "UNKNOWN", "reason": f"AI Error: {e}", "source": "gemini"}
