"""
자동 전략 최적화 시스템 (Auto Strategy Optimizer)

시장 상황을 분석하여 최적의 전략/모드/페르소나를 자동으로 결정합니다.
사용자가 직접 선택할 필요 없이 수익 최대화, 손실 최소화를 목표로 합니다.

분석 요소:
1. AI 합의 (Multi-LLM): market_condition, risk_level
2. 기술적 지표: 주요 지수의 MA 트렌드, 변동성, 갭다운, 연속하락
3. 최근 수익률 추적 (profit_tracker 연동)
4. 계좌 상태: 자산 규모, 보유 포지션 수

전략 매핑:
- DCA: 횡보/불확실한 시장 → 분할매수로 평단가 관리
- Swing: 명확한 상승/하락 추세 → 추세 추종 보유
- Day: 고변동성, 방향 불확실 → 당일 매매로 리스크 최소화

모드 매핑:
- Safe: 하락장, 고위험, 소규모 계좌 → 1X 종목만
- Risky: 상승장, 저위험, 충분한 자산 → 레버리지 ETF 포함

페르소나 매핑:
- Aggressive: 강세장, 명확한 매수 시그널
- Neutral: 혼조세, 불확실한 방향성
- Conservative: 약세장, 고위험 경고
"""

import json
import os
import datetime
from modules.logger import logger

# 전략 결정 히스토리 저장 파일
STRATEGY_HISTORY_FILE = "database/strategy_history.json"


class AutoStrategyOptimizer:
    """시장 상황 기반 자동 전략 최적화"""
    
    def __init__(self, config_file="user_config.json"):
        self.config_file = config_file
        self.history = self._load_history()
    
    def _load_history(self):
        """전략 변경 히스토리 로드"""
        try:
            if os.path.exists(STRATEGY_HISTORY_FILE):
                with open(STRATEGY_HISTORY_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[AutoStrategy] 히스토리 로드 실패: {e}")
        return {"changes": [], "last_strategy": None, "last_mode": None, "last_persona": None}
    
    def _save_history(self):
        """전략 변경 히스토리 저장"""
        try:
            os.makedirs(os.path.dirname(STRATEGY_HISTORY_FILE), exist_ok=True)
            with open(STRATEGY_HISTORY_FILE, "w") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[AutoStrategy] 히스토리 저장 실패: {e}")
    
    def _load_config(self):
        try:
            with open(self.config_file, "r") as f:
                return json.load(f)
        except:
            return {}
    
    def _save_config(self, config):
        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

    def analyze_market_signals(self, market, kis, ohlc_data_map=None):
        """
        시장 기술적 지표를 분석하여 종합 시그널을 생성합니다.
        
        Args:
            market: 'US' or 'KR'
            kis: KIS API 인스턴스
            ohlc_data_map: {ticker: ohlc_list} 형태의 사전 분석된 데이터 (선택)
        
        Returns:
            dict: {
                'trend_score': float (-1.0 ~ 1.0, 음수=하락, 양수=상승),
                'volatility_score': float (0.0 ~ 1.0, 높을수록 변동성 큼),
                'momentum_score': float (-1.0 ~ 1.0, 음수=약세, 양수=강세),
                'risk_score': float (0.0 ~ 1.0, 높을수록 위험),
                'num_uptrend': int,
                'num_total': int,
                'details': str
            }
        """
        # 대표 지수 종목 (시장 전체 흐름 파악용)
        if market == 'US':
            index_tickers = [
                {'symbol': 'TQQQ', 'exchange': 'NAS'},  # 나스닥 3X → 나스닥 흐름
                {'symbol': 'NVDA', 'exchange': 'NAS'},   # 대형 테크
                {'symbol': 'AAPL', 'exchange': 'NAS'},   # 대형 가치
            ]
        else:
            index_tickers = [
                '005930',  # 삼성전자 → KOSPI 대표
                '000660',  # SK하이닉스 → 반도체
                '035420',  # 네이버 → 성장주
            ]
        
        trend_scores = []
        volatility_scores = []
        momentum_scores = []
        details = []
        
        for t_obj in index_tickers:
            try:
                if isinstance(t_obj, dict):
                    ticker = t_obj['symbol']
                    exchange = t_obj['exchange']
                    ohlc = kis.get_daily_ohlc(ticker, exchange) if not ohlc_data_map else ohlc_data_map.get(ticker)
                else:
                    ticker = t_obj
                    exchange = None
                    ohlc = kis.get_daily_ohlc(ticker) if not ohlc_data_map else ohlc_data_map.get(ticker)
                
                if not ohlc or len(ohlc) < 10:
                    continue
                
                closes = [float(x['clos']) for x in ohlc]
                closes.reverse()
                
                highs = [float(x['high']) for x in ohlc]
                highs.reverse()
                
                lows = [float(x['low']) for x in ohlc]
                lows.reverse()
                
                current = closes[-1] if closes else 0
                
                # 1. 트렌드 점수: MA20 대비 위치 + MA5 대비 위치
                ma20 = sum(closes[-20:]) / min(len(closes), 20) if len(closes) >= 5 else current
                ma5 = sum(closes[-5:]) / min(len(closes[-5:]), 5)
                
                # MA20 대비 (-5% ~ +5% → -1.0 ~ 1.0)
                ma20_dist = (current - ma20) / ma20 if ma20 > 0 else 0
                trend_s = max(-1.0, min(1.0, ma20_dist / 0.05))
                
                # MA5 방향 (최근 5일 이동평균의 기울기)
                if len(closes) >= 7:
                    ma5_prev = sum(closes[-7:-2]) / 5
                    ma5_direction = (ma5 - ma5_prev) / ma5_prev if ma5_prev > 0 else 0
                    trend_s += max(-0.5, min(0.5, ma5_direction / 0.02))
                
                # 최종 범위 제한
                trend_s = max(-1.0, min(1.0, trend_s))
                trend_scores.append(trend_s)
                
                # 2. 변동성 점수: ATR (Average True Range)
                if len(highs) >= 5 and len(lows) >= 5:
                    true_ranges = []
                    for i in range(-5, 0):
                        tr = highs[i] - lows[i]
                        if i > -5 and closes[i-1] > 0:
                            tr = max(tr, abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                        true_ranges.append(tr)
                    atr = sum(true_ranges) / len(true_ranges)
                    atr_pct = (atr / current * 100) if current > 0 else 0
                    # ATR 0~5% → 0.0~1.0
                    vol_s = min(1.0, atr_pct / 5.0)
                else:
                    vol_s = 0.3  # 기본값
                
                volatility_scores.append(vol_s)
                
                # 3. 모멘텀 점수: 최근 5일 수익률
                if len(closes) >= 6:
                    returns_5d = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] > 0 else 0
                    mom_s = max(-1.0, min(1.0, returns_5d / 0.05))
                else:
                    mom_s = 0.0
                
                momentum_scores.append(mom_s)
                
                details.append(f"{ticker}: trend={trend_s:.2f}, vol={vol_s:.2f}, mom={mom_s:.2f}")
                
            except Exception as e:
                logger.warning(f"[AutoStrategy] {t_obj} 분석 실패: {e}")
                continue
        
        if not trend_scores:
            return {
                'trend_score': 0.0,
                'volatility_score': 0.3,
                'momentum_score': 0.0,
                'risk_score': 0.5,
                'num_uptrend': 0,
                'num_total': 0,
                'details': 'No data available'
            }
        
        avg_trend = sum(trend_scores) / len(trend_scores)
        avg_vol = sum(volatility_scores) / len(volatility_scores)
        avg_mom = sum(momentum_scores) / len(momentum_scores)
        num_uptrend = sum(1 for s in trend_scores if s > 0)
        
        # 종합 리스크 점수 (높을수록 위험)
        # 하락 추세 + 고변동성 + 약세 모멘텀 → 위험
        risk_score = 0.0
        risk_score += max(0, -avg_trend) * 0.4       # 하락 추세 반영
        risk_score += avg_vol * 0.3                    # 변동성 반영
        risk_score += max(0, -avg_mom) * 0.3          # 약세 모멘텀 반영
        risk_score = min(1.0, risk_score)
        
        return {
            'trend_score': round(avg_trend, 3),
            'volatility_score': round(avg_vol, 3),
            'momentum_score': round(avg_mom, 3),
            'risk_score': round(risk_score, 3),
            'num_uptrend': num_uptrend,
            'num_total': len(trend_scores),
            'details': '; '.join(details)
        }

    def determine_optimal_strategy(self, market, ai_sentiment, tech_signals, 
                                    total_asset_krw=0, num_holdings=0,
                                    leverage_threshold=10_000_000):
        """
        모든 시그널을 종합하여 최적 전략을 결정합니다.
        
        Args:
            market: 'US' or 'KR'
            ai_sentiment: MultiLLM 합의 결과 dict (market_condition, risk_level, can_buy 등)
            tech_signals: analyze_market_signals() 결과 dict
            total_asset_krw: 총 자산 (원화 기준)
            num_holdings: 현재 보유 종목 수
            leverage_threshold: 레버리지 모드 전환 기준 자산
        
        Returns:
            dict: {
                'strategy': 'day' | 'swing' | 'dca',
                'trading_mode': 'safe' | 'risky',
                'persona': 'aggressive' | 'neutral' | 'conservative',
                'reason': str,
                'confidence': float (0.0 ~ 1.0)
            }
        """
        trend = tech_signals.get('trend_score', 0)
        volatility = tech_signals.get('volatility_score', 0.3)
        momentum = tech_signals.get('momentum_score', 0)
        risk = tech_signals.get('risk_score', 0.5)
        
        ai_condition = (ai_sentiment or {}).get('market_condition', 'NEUTRAL')
        ai_risk = (ai_sentiment or {}).get('risk_level', 'LOW')
        ai_can_buy = (ai_sentiment or {}).get('can_buy', True)
        
        reasons = []
        confidence = 0.5
        
        # ============================================
        # 1. 전략 (Strategy) 결정
        # ============================================
        strategy = 'dca'  # 기본값: 가장 안전한 DCA
        
        # --- CRASH / 극단적 하락 → Day Trading (당일 청산으로 리스크 제한) ---
        if ai_condition == 'CRASH':
            strategy = 'day'
            reasons.append("CRASH 감지 → Day Trading (당일 청산)")
            confidence = 0.9
        
        # --- 강한 상승 추세 + 낮은 변동성 → Swing (추세 추종 보유) ---
        elif trend > 0.5 and momentum > 0.3 and volatility < 0.6:
            strategy = 'swing'
            reasons.append(f"강한 상승 추세(trend={trend:.2f}) + 양호한 모멘텀 → Swing")
            confidence = 0.8
        
        # --- 상승 추세 + 높은 변동성 → Day (변동성 활용 당일매매) ---
        elif trend > 0.2 and volatility > 0.6:
            strategy = 'day'
            reasons.append(f"상승세 but 고변동성(vol={volatility:.2f}) → Day Trading")
            confidence = 0.7
        
        # --- 하락 추세 + 높은 변동성 → Day (빠른 탈출 가능) ---
        elif trend < -0.3 and volatility > 0.5:
            strategy = 'day'
            reasons.append(f"하락 추세(trend={trend:.2f}) + 고변동성 → Day Trading (방어)")
            confidence = 0.75
        
        # --- 완만한 상승/횡보 → DCA (분할매수로 평단가 관리) ---
        elif -0.2 <= trend <= 0.5:
            strategy = 'dca'
            reasons.append(f"횡보/완만한 추세(trend={trend:.2f}) → DCA (분할매수)")
            confidence = 0.65
        
        # --- 하락 추세 + 낮은 변동성 → DCA (저가 분할매수 기회) ---
        elif trend < -0.2 and volatility < 0.4:
            strategy = 'dca'
            reasons.append(f"완만한 하락(trend={trend:.2f}) + 저변동성 → DCA (저가 매수)")
            confidence = 0.6
        
        # --- 그 외 → DCA (안전 기본값) ---
        else:
            strategy = 'dca'
            reasons.append(f"불확실한 시장상황 → DCA (기본 안전 전략)")
            confidence = 0.5
        
        # ============================================
        # 2. 트레이딩 모드 (Trading Mode) 결정
        # ============================================
        trading_mode = 'safe'  # 기본값
        
        # KR 시장: 자산 규모 + 시장 상황 기반
        if market == 'KR':
            if total_asset_krw >= leverage_threshold:
                # 자산 충분 → 시장 좋으면 레버리지
                if ai_condition in ('BULLISH',) and trend > 0.3 and risk < 0.4:
                    trading_mode = 'risky'
                    reasons.append(f"KR 자산 {total_asset_krw:,.0f}원 + 강세장 → 레버리지 모드")
                elif ai_condition == 'NEUTRAL' and trend > 0 and momentum > 0:
                    trading_mode = 'risky'
                    reasons.append(f"KR 자산 충분 + 양호한 흐름 → 레버리지 모드")
                else:
                    trading_mode = 'safe'
                    reasons.append(f"KR 자산 충분하나 시장 불안정 → 안전 모드 유지")
            else:
                trading_mode = 'safe'
                reasons.append(f"KR 자산 {total_asset_krw:,.0f}원 (기준 미달) → 안전 모드")
        else:
            # US 시장: 모드가 영향 없음 (항상 3X+1X 전부 사용)
            # 하지만 위험할 때는 safe로 기록 (향후 확장 대비)
            if risk > 0.7 or ai_condition in ('CRASH', 'BEARISH'):
                trading_mode = 'safe'
                reasons.append("US 고위험 → Safe 기록")
            else:
                trading_mode = 'risky'
                reasons.append("US 정상 → Risky 기록")
        
        # ============================================
        # 3. 페르소나 (Persona) 결정
        # ============================================
        if ai_condition in ('CRASH', 'BEARISH') or risk > 0.6:
            persona = 'conservative'
            reasons.append(f"위험 감지(risk={risk:.2f}) → Conservative 페르소나")
        elif ai_condition == 'BULLISH' and risk < 0.3 and trend > 0.3:
            persona = 'aggressive'
            reasons.append(f"강세장 + 저위험 → Aggressive 페르소나")
        else:
            persona = 'neutral'
            reasons.append(f"혼조세 → Neutral 페르소나")
        
        # ============================================
        # 4. 보유 포지션 반영 보정
        # ============================================
        # 이미 많은 포지션 보유 중이면 보수적으로
        if num_holdings >= 5:
            if persona == 'aggressive':
                persona = 'neutral'
                reasons.append(f"보유 {num_holdings}종목 → Neutral로 하향")
            confidence *= 0.9
        
        # AI가 매수 불가 판정이면 보수적으로 보정
        if not ai_can_buy:
            if persona == 'aggressive':
                persona = 'neutral'
            if strategy == 'swing':
                strategy = 'dca'
                reasons.append("AI 매수불가 → Swing에서 DCA로 전환")
        
        return {
            'strategy': strategy,
            'trading_mode': trading_mode,
            'persona': persona,
            'reason': ' | '.join(reasons),
            'confidence': round(confidence, 2),
            'signals': {
                'trend': trend,
                'volatility': volatility,
                'momentum': momentum,
                'risk': risk,
                'ai_condition': ai_condition,
                'ai_risk': ai_risk,
                'ai_can_buy': ai_can_buy
            }
        }

    def apply_strategy(self, decision, market):
        """
        결정된 전략을 config에 적용하고 히스토리에 기록합니다.
        
        Args:
            decision: determine_optimal_strategy() 결과
            market: 'US' or 'KR'
        
        Returns:
            dict: 변경 사항 요약 {'changed': bool, 'changes': list}
        """
        config = self._load_config()
        
        old_strategy = config.get('strategy', 'day')
        old_mode = config.get('trading_mode', 'safe')
        old_persona = config.get('persona', 'aggressive')
        
        new_strategy = decision['strategy']
        new_mode = decision['trading_mode']
        new_persona = decision['persona']
        
        changes = []
        
        if old_strategy != new_strategy:
            changes.append(f"전략: {old_strategy} → {new_strategy}")
        if old_mode != new_mode:
            changes.append(f"모드: {old_mode} → {new_mode}")
        if old_persona != new_persona:
            changes.append(f"페르소나: {old_persona} → {new_persona}")
        
        changed = len(changes) > 0
        
        if changed:
            config['strategy'] = new_strategy
            config['trading_mode'] = new_mode
            config['persona'] = new_persona
            self._save_config(config)
            
            # 히스토리 기록
            record = {
                'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'market': market,
                'old': {'strategy': old_strategy, 'mode': old_mode, 'persona': old_persona},
                'new': {'strategy': new_strategy, 'mode': new_mode, 'persona': new_persona},
                'reason': decision.get('reason', ''),
                'confidence': decision.get('confidence', 0),
                'signals': decision.get('signals', {})
            }
            self.history['changes'].append(record)
            # 최근 100건만 유지
            if len(self.history['changes']) > 100:
                self.history['changes'] = self.history['changes'][-100:]
            
            self.history['last_strategy'] = new_strategy
            self.history['last_mode'] = new_mode
            self.history['last_persona'] = new_persona
            self._save_history()
            
            logger.info(f"[AutoStrategy] ✅ 전략 변경 적용: {', '.join(changes)}")
            logger.info(f"[AutoStrategy] 사유: {decision.get('reason', '')}")
            logger.info(f"[AutoStrategy] 신뢰도: {decision.get('confidence', 0):.0%}")
        else:
            logger.info(f"[AutoStrategy] 현재 전략 유지: strategy={new_strategy}, mode={new_mode}, persona={new_persona}")
        
        return {
            'changed': changed,
            'changes': changes,
            'current': {
                'strategy': new_strategy,
                'trading_mode': new_mode,
                'persona': new_persona
            }
        }

    def optimize(self, market, kis, ai_sentiment=None, total_asset_krw=0, 
                 num_holdings=0, leverage_threshold=10_000_000):
        """
        전체 최적화 파이프라인을 실행합니다.
        
        이 함수 하나만 호출하면 시장분석 → 전략결정 → config 적용까지 완료됩니다.
        
        Args:
            market: 'US' or 'KR'
            kis: KIS API 인스턴스
            ai_sentiment: MultiLLM 분석 결과 (없으면 기술적 분석만 사용)
            total_asset_krw: 원화 환산 총자산
            num_holdings: 보유 종목 수
            leverage_threshold: 레버리지 모드 전환 기준
        
        Returns:
            dict: {
                'changed': bool,
                'changes': list,
                'decision': dict,
                'signals': dict,
                'current': dict
            }
        """
        logger.info(f"[AutoStrategy] 🔍 {market} 시장 자동 전략 최적화 시작...")
        
        # 1. 기술적 지표 분석
        tech_signals = self.analyze_market_signals(market, kis)
        logger.info(f"[AutoStrategy] 기술 분석: trend={tech_signals['trend_score']}, "
                     f"vol={tech_signals['volatility_score']}, mom={tech_signals['momentum_score']}, "
                     f"risk={tech_signals['risk_score']}")
        logger.info(f"[AutoStrategy] 상세: {tech_signals['details']}")
        
        # 2. 최적 전략 결정
        decision = self.determine_optimal_strategy(
            market=market,
            ai_sentiment=ai_sentiment,
            tech_signals=tech_signals,
            total_asset_krw=total_asset_krw,
            num_holdings=num_holdings,
            leverage_threshold=leverage_threshold
        )
        logger.info(f"[AutoStrategy] 결정: strategy={decision['strategy']}, "
                     f"mode={decision['trading_mode']}, persona={decision['persona']}")
        logger.info(f"[AutoStrategy] 사유: {decision['reason']}")
        
        # 3. config에 적용
        result = self.apply_strategy(decision, market)
        result['decision'] = decision
        result['signals'] = tech_signals
        
        return result

    def get_history(self, limit=20):
        """최근 전략 변경 히스토리 반환"""
        changes = self.history.get('changes', [])
        return changes[-limit:] if len(changes) > limit else changes
    
    def get_current_auto_status(self):
        """현재 자동 전략 상태 요약"""
        return {
            'last_strategy': self.history.get('last_strategy'),
            'last_mode': self.history.get('last_mode'),
            'last_persona': self.history.get('last_persona'),
            'total_changes': len(self.history.get('changes', [])),
            'recent_changes': self.get_history(5)
        }
