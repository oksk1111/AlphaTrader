"""
다중 LLM 합의 시스템 (Multi-LLM Consensus Engine)

여러 무료 LLM의 의견을 수렴하여 다수결로 투자 판단을 내립니다.
- Gemini (Google): 뉴스 해석, 영어 역량
- Grok (xAI): X(트위터) 기반 실시간 분석
- DeepSeek: 아시아 시장 관점
- Groq (Llama): 초고속 추론, 보조 의견

합의 방식:
1. 각 LLM이 독립적으로 risk_level + can_buy 판단
2. 다수결 투표 (Majority Vote)
3. CRASH 판정이 하나라도 있으면 매수 차단
"""

import concurrent.futures
from modules.gemini_analyst import GeminiAnalyst
from modules.grok_analyst import GrokAnalyst
from modules.deepseek_analyst import DeepSeekAnalyst
from modules.groq_analyst import GroqAnalyst
from modules.logger import logger


class MultiLLMAnalyst:
    def __init__(self):
        self.analysts = []
        self.analyst_names = []
        
        # Gemini (기본, 필수)
        gemini = GeminiAnalyst()
        self.analysts.append(gemini)
        self.analyst_names.append("Gemini")
        
        # Grok (선택)
        grok = GrokAnalyst()
        if grok.available:
            self.analysts.append(grok)
            self.analyst_names.append("Grok")
        
        # DeepSeek (선택)
        deepseek = DeepSeekAnalyst()
        if deepseek.available:
            self.analysts.append(deepseek)
            self.analyst_names.append("DeepSeek")
        
        # Groq (선택)
        groq = GroqAnalyst()
        if groq.available:
            self.analysts.append(groq)
            self.analyst_names.append("Groq")
        
        logger.info(f"[MultiLLM] 활성 LLM: {self.analyst_names} ({len(self.analysts)}개)")

    def fetch_news(self):
        """뉴스 수집 (Gemini의 fetch_news 사용)"""
        # Gemini가 뉴스 수집 전담
        gemini = self.analysts[0]  # Gemini는 항상 첫 번째
        if hasattr(gemini, 'fetch_news'):
            return gemini.fetch_news()
        return ""

    def _query_single_llm(self, analyst, news_text, persona):
        """단일 LLM에 질의"""
        try:
            result = analyst.check_market_sentiment(news_text, persona=persona)
            return result
        except Exception as e:
            name = getattr(analyst, '__class__', type(analyst)).__name__
            logger.error(f"[MultiLLM] {name} query failed: {e}")
            return None

    def check_market_sentiment(self, news_text, persona="aggressive"):
        """
        다중 LLM 합의 기반 시장 감성 분석
        
        합의 규칙:
        1. 사용 가능한 LLM들에게 병렬로 질의
        2. CRASH 판정이 하나라도 있으면 매수 차단
        3. 나머지는 다수결 투표 (can_buy 기준)
        4. LLM 1개만 활성이면 해당 LLM의 판단을 따름
        """
        if not news_text:
            return {
                "risk_level": "LOW", 
                "can_buy": True,
                "market_condition": "NEUTRAL",
                "reason": "No news available, skipping AI check.",
                "consensus": "N/A",
                "votes": {}
            }
        
        # 병렬로 모든 LLM 질의
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self._query_single_llm, analyst, news_text, persona): name
                for analyst, name in zip(self.analysts, self.analyst_names)
            }
            
            for future in concurrent.futures.as_completed(futures, timeout=30):
                name = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        # API 에러(UNKNOWN)는 투표에서 제외 - 실패한 LLM이 반대표로 집계되는 것 방지
                        if result.get('risk_level') == 'UNKNOWN':
                            logger.warning(f"[MultiLLM] {name}: API 에러로 투표 제외 - {result.get('reason', 'N/A')[:80]}")
                            continue
                        result["_name"] = name
                        results.append(result)
                        logger.info(f"[MultiLLM] {name}: risk={result.get('risk_level')}, buy={result.get('can_buy')}, reason={result.get('reason', 'N/A')[:50]}")
                except Exception as e:
                    logger.error(f"[MultiLLM] {name} 결과 처리 실패: {e}")
        
        if not results:
            logger.warning("[MultiLLM] 모든 LLM 응답 실패! 안전을 위해 매수 차단.")
            return {
                "risk_level": "UNKNOWN",
                "can_buy": False,
                "market_condition": "UNKNOWN",
                "reason": "All LLMs failed to respond.",
                "consensus": "ALL_FAILED",
                "votes": {}
            }
        
        # 합의 처리
        votes = {}
        buy_votes = 0
        no_buy_votes = 0
        has_crash = False
        reasons = []
        
        for r in results:
            name = r.get("_name", "Unknown")
            can_buy = r.get("can_buy", False)
            risk = r.get("risk_level", "UNKNOWN")
            condition = r.get("market_condition", "")
            reason = r.get("reason", "")
            
            votes[name] = {
                "can_buy": can_buy,
                "risk_level": risk,
                "reason": reason[:100]
            }
            
            if can_buy:
                buy_votes += 1
            else:
                no_buy_votes += 1
            
            if condition == "CRASH":
                has_crash = True
                logger.warning(f"[MultiLLM] ⚠️ {name}이 CRASH 판정! 매수 차단.")
            
            reasons.append(f"{name}: {reason[:50]}")
        
        total_votes = buy_votes + no_buy_votes
        
        # CRASH가 하나라도 있으면 무조건 차단
        if has_crash:
            consensus_result = {
                "risk_level": "HIGH",
                "can_buy": False,
                "market_condition": "CRASH",
                "reason": f"CRASH detected by LLM. {'; '.join(reasons)}",
                "consensus": f"CRASH_VETO ({buy_votes}/{total_votes} buy votes, but CRASH detected)",
                "votes": votes
            }
        # 다수결
        elif buy_votes > no_buy_votes:
            consensus_result = {
                "risk_level": "LOW",
                "can_buy": True,
                "market_condition": "BULLISH" if buy_votes == total_votes else "NEUTRAL",
                "reason": f"Majority buy ({buy_votes}/{total_votes}). {'; '.join(reasons)}",
                "consensus": f"BUY ({buy_votes}/{total_votes})",
                "votes": votes
            }
        elif buy_votes == no_buy_votes:
            # 동률: persona에 따라 결정
            if persona == "aggressive":
                final_buy = True
                consensus_type = "TIE_BUY (aggressive persona)"
            else:
                final_buy = False
                consensus_type = "TIE_HOLD (conservative/neutral persona)"
            
            consensus_result = {
                "risk_level": "LOW" if final_buy else "HIGH",
                "can_buy": final_buy,
                "market_condition": "NEUTRAL",
                "reason": f"Tie vote ({buy_votes}/{total_votes}). {'; '.join(reasons)}",
                "consensus": consensus_type,
                "votes": votes
            }
        else:
            consensus_result = {
                "risk_level": "HIGH",
                "can_buy": False,
                "market_condition": "BEARISH",
                "reason": f"Majority hold ({no_buy_votes}/{total_votes}). {'; '.join(reasons)}",
                "consensus": f"HOLD ({no_buy_votes}/{total_votes})",
                "votes": votes
            }
        
        logger.info(f"[MultiLLM] 합의 결과: {consensus_result['consensus']} → can_buy={consensus_result['can_buy']}")
        return consensus_result
