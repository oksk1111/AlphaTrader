# 📱 텔레그램 알림 설정 가이드

Alpha Trader에 텔레그램 알림을 연동하는 방법입니다.

## 1️⃣ 텔레그램 봇 생성

### Step 1: BotFather에서 봇 만들기
1. 텔레그램에서 [@BotFather](https://t.me/botfather) 검색
2. `/newbot` 명령어 입력
3. 봇 이름 입력 (예: `Alpha Trader Alert`)
4. 봇 사용자명 입력 (예: `alpha_trader_bot`) - `_bot`으로 끝나야 함
5. **Bot Token** 복사 (예: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Step 2: Chat ID 확인
1. 생성한 봇을 검색하여 `/start` 전송
2. 브라우저에서 아래 URL 접속 (TOKEN을 실제 토큰으로 변경):
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. 응답에서 `"chat":{"id":123456789}` 부분의 숫자가 **Chat ID**

## 2️⃣ Oracle Cloud 서버에 환경변수 설정

SSH로 서버 접속 후:

```bash
# .bashrc 또는 .profile에 추가
echo 'export TELEGRAM_BOT_TOKEN="your_bot_token_here"' >> ~/.bashrc
echo 'export TELEGRAM_CHAT_ID="your_chat_id_here"' >> ~/.bashrc

# 즉시 적용
source ~/.bashrc
```

또는 systemd 서비스에 직접 추가:

```bash
sudo nano /etc/systemd/system/alphatrader.service
```

`[Service]` 섹션에 추가:
```ini
Environment="TELEGRAM_BOT_TOKEN=your_bot_token_here"
Environment="TELEGRAM_CHAT_ID=your_chat_id_here"
```

## 3️⃣ 테스트

```bash
cd /home/mingky/workspace/AlphaTrader
source venv/bin/activate

# 알림 테스트
python -c "
from modules.telegram_notifier import TelegramNotifier
notifier = TelegramNotifier()
if notifier.is_configured():
    notifier.send_message('🔔 Alpha Trader 텔레그램 알림 테스트입니다!')
    print('✅ 테스트 메시지 발송 완료!')
else:
    print('❌ 텔레그램이 설정되지 않았습니다.')
"
```

## 4️⃣ 알림 종류

| 알림 유형 | 발송 시점 | 내용 |
|----------|----------|------|
| 📊 일일 보고서 | 매일 16:00 | 계좌 현황, 거래 내역, 보유 종목 |
| 🚨 장애 알림 | Bot 재시작 3회 실패 시 | 긴급 조치 필요 알림 |

## 5️⃣ 설정 옵션 (user_config.json)

```json
{
    "telegram": {
        "enabled": true,
        "daily_report_hour": 16,
        "alert_on_trade": false
    }
}
```

- `enabled`: 텔레그램 알림 활성화 여부
- `daily_report_hour`: 일일 보고서 발송 시간 (24시간 형식)
- `alert_on_trade`: 거래 체결 시 알림 (선택)

## 🔧 트러블슈팅

### 알림이 오지 않는 경우
1. 환경변수 확인: `echo $TELEGRAM_BOT_TOKEN`
2. 봇에 먼저 `/start` 메시지를 보냈는지 확인
3. Chat ID가 정확한지 확인

### Chat ID 재확인
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | jq
```

---

## 📋 빠른 체크리스트

- [ ] BotFather에서 봇 생성
- [ ] Bot Token 복사
- [ ] 봇에 `/start` 전송
- [ ] Chat ID 확인
- [ ] 서버에 환경변수 설정
- [ ] 테스트 메시지 발송 확인
