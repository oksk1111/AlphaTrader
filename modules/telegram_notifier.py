"""
텔레그램 알림 모듈
- 일일 거래 보고서 발송
- Bot 장애 알림
"""

import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path

class TelegramNotifier:
    def __init__(self, bot_token: str = None, chat_id: str = None):
        """
        텔레그램 알림 초기화
        
        Args:
            bot_token: 텔레그램 봇 토큰 (환경변수 TELEGRAM_BOT_TOKEN으로도 설정 가능)
            chat_id: 알림받을 채팅 ID (환경변수 TELEGRAM_CHAT_ID로도 설정 가능)
        """
        self.bot_token = bot_token or os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = chat_id or os.getenv('TELEGRAM_CHAT_ID')
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.database_dir = Path(__file__).parent.parent / "database"
        
    def is_configured(self) -> bool:
        """텔레그램 설정이 완료되었는지 확인"""
        return bool(self.bot_token and self.chat_id)
    
    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        텔레그램 메시지 발송
        
        Args:
            message: 발송할 메시지
            parse_mode: 메시지 형식 (HTML, Markdown, MarkdownV2)
            
        Returns:
            성공 여부
        """
        if not self.is_configured():
            print("⚠️ Telegram not configured. Skipping notification.")
            return False
            
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                return True
            else:
                print(f"❌ Telegram API error: {response.text}")
                return False
                
        except Exception as e:
            print(f"❌ Failed to send Telegram message: {e}")
            return False
    
    def get_today_trades(self) -> list:
        """오늘 거래 내역 조회"""
        trades = []
        trade_file = self.database_dir / "trades.json"
        
        if trade_file.exists():
            try:
                with open(trade_file, 'r', encoding='utf-8') as f:
                    all_trades = json.load(f)
                    
                today = datetime.now().strftime('%Y-%m-%d')
                trades = [t for t in all_trades if t.get('date', '').startswith(today)]
            except Exception as e:
                print(f"Error reading trades: {e}")
                
        return trades
    
    def get_balance_info(self) -> dict:
        """현재 잔고 정보 조회"""
        balance_file = self.database_dir / "balance_snapshot.json"
        
        if balance_file.exists():
            try:
                with open(balance_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
                
        return {}
    
    def get_bot_status(self) -> dict:
        """봇 상태 정보 조회"""
        import subprocess
        
        status = {
            "bot_running": False,
            "dashboard_running": False,
            "last_log_time": None,
            "last_log_message": None
        }
        
        # 프로세스 상태 확인
        try:
            bot_check = subprocess.run(
                ["pgrep", "-f", "python.*run_bot.py"],
                capture_output=True, text=True
            )
            status["bot_running"] = bot_check.returncode == 0
            
            dashboard_check = subprocess.run(
                ["pgrep", "-f", "streamlit.*dashboard.py"],
                capture_output=True, text=True
            )
            status["dashboard_running"] = dashboard_check.returncode == 0
        except:
            pass
        
        # 최근 로그 확인
        log_file = self.database_dir / f"trading_{datetime.now().strftime('%Y%m%d')}.log"
        if log_file.exists():
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    if lines:
                        last_line = lines[-1].strip()
                        status["last_log_message"] = last_line[:100]
                        # 로그 시간 파싱
                        try:
                            time_str = last_line.split(' - ')[0]
                            status["last_log_time"] = time_str
                        except:
                            pass
            except:
                pass
                
        return status
    
    def get_holdings(self) -> list:
        """현재 보유 종목 조회"""
        holdings_file = self.database_dir / "holdings.json"
        
        if holdings_file.exists():
            try:
                with open(holdings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
                
        return []
    
    def calculate_daily_pnl(self, trades: list) -> dict:
        """일일 손익 계산"""
        result = {
            "total_buy_amount": 0,
            "total_sell_amount": 0,
            "realized_pnl": 0,
            "buy_count": 0,
            "sell_count": 0
        }
        
        for trade in trades:
            amount = trade.get('amount', 0)
            trade_type = trade.get('type', '').upper()
            
            if trade_type == 'BUY':
                result["total_buy_amount"] += amount
                result["buy_count"] += 1
            elif trade_type == 'SELL':
                result["total_sell_amount"] += amount
                result["sell_count"] += 1
                
        result["realized_pnl"] = result["total_sell_amount"] - result["total_buy_amount"]
        
        return result
    
    def generate_daily_report(self) -> str:
        """일일 거래 보고서 생성"""
        now = datetime.now()
        
        # 데이터 수집
        trades = self.get_today_trades()
        balance = self.get_balance_info()
        bot_status = self.get_bot_status()
        holdings = self.get_holdings()
        pnl = self.calculate_daily_pnl(trades)
        
        # 보고서 생성
        report = f"""
📊 <b>Alpha Trader 일일 보고서</b>
━━━━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y년 %m월 %d일 %H:%M')}

<b>🤖 Bot 상태</b>
├ 봇: {'🟢 Running' if bot_status['bot_running'] else '🔴 Stopped'}
├ 대시보드: {'🟢 Running' if bot_status['dashboard_running'] else '🔴 Stopped'}
└ 마지막 로그: {bot_status.get('last_log_time', 'N/A')}

<b>💰 계좌 현황</b>
├ 총 평가금액: {balance.get('total_assets', 0):,.0f} 원
├ 예수금: {balance.get('cash', 0):,.0f} 원
└ 주식 평가: {balance.get('stock_value', 0):,.0f} 원

<b>📈 오늘의 거래</b>
├ 매수: {pnl['buy_count']}건 ({pnl['total_buy_amount']:,.0f} 원)
├ 매도: {pnl['sell_count']}건 ({pnl['total_sell_amount']:,.0f} 원)
└ 실현손익: {pnl['realized_pnl']:+,.0f} 원

<b>📦 보유 종목 ({len(holdings)}개)</b>
"""
        
        if holdings:
            for h in holdings[:5]:  # 최대 5개만 표시
                ticker = h.get('ticker', 'N/A')
                qty = h.get('quantity', 0)
                pnl_rate = h.get('pnl_rate', 0)
                emoji = "📈" if pnl_rate >= 0 else "📉"
                report += f"├ {ticker}: {qty}주 ({emoji} {pnl_rate:+.2f}%)\n"
            if len(holdings) > 5:
                report += f"└ ... 외 {len(holdings) - 5}개\n"
        else:
            report += "└ 보유 종목 없음\n"
        
        report += """
━━━━━━━━━━━━━━━━━━━━
🔗 Dashboard: http://158.180.81.25:8501
"""
        
        return report
    
    def send_daily_report(self) -> bool:
        """일일 보고서 발송"""
        report = self.generate_daily_report()
        return self.send_message(report)
    
    def send_bot_failure_alert(self, restart_attempts: int = 0, error_msg: str = None) -> bool:
        """봇 장애 알림 발송"""
        now = datetime.now()
        
        alert = f"""
🚨 <b>Alpha Trader 긴급 알림</b>
━━━━━━━━━━━━━━━━━━━━
⏰ {now.strftime('%Y-%m-%d %H:%M:%S')}

<b>❌ Bot 재시작 실패!</b>

├ 재시작 시도: {restart_attempts}회
├ 상태: 🔴 STOPPED
└ 즉시 확인이 필요합니다!

{f'<b>에러 메시지:</b>\n<code>{error_msg[:200]}</code>' if error_msg else ''}

<b>조치 방법:</b>
1. SSH 접속: ssh user@158.180.81.25
2. 로그 확인: tail -f database/trading_*.log
3. 수동 시작: ./auto_restart_bot.sh

🔗 Dashboard: http://158.180.81.25:8501
━━━━━━━━━━━━━━━━━━━━
"""
        return self.send_message(alert)
    
    def send_trade_alert(self, trade_type: str, ticker: str, quantity: int, 
                         price: float, amount: float) -> bool:
        """거래 알림 발송 (선택적)"""
        emoji = "🟢" if trade_type.upper() == "BUY" else "🔴"
        
        alert = f"""
{emoji} <b>{trade_type.upper()}</b> 체결
━━━━━━━━━━━━━━
종목: {ticker}
수량: {quantity}주
가격: {price:,.0f} 원
금액: {amount:,.0f} 원
시간: {datetime.now().strftime('%H:%M:%S')}
"""
        return self.send_message(alert)


# 단일 인스턴스 생성 (모듈 import 시 자동 초기화)
def get_notifier() -> TelegramNotifier:
    """TelegramNotifier 인스턴스 반환"""
    return TelegramNotifier()


if __name__ == "__main__":
    # 테스트
    notifier = TelegramNotifier()
    
    if notifier.is_configured():
        print("✅ Telegram configured. Sending test message...")
        notifier.send_message("🔔 Alpha Trader 텔레그램 알림 테스트입니다!")
    else:
        print("⚠️ Telegram not configured.")
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")
