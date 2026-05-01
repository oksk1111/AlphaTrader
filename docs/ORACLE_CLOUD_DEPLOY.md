# Oracle Cloud 자동 재시작 배포 가이드

## 🚀 빠른 시작

Oracle Cloud 인스턴스에서 봇을 자동 재시작되도록 설정하는 방법입니다.

### 1. 자동 재시작 스크립트 실행 권한 부여

```bash
chmod +x auto_restart_bot.sh
```

### 2. Systemd 서비스 설치 (권장)

```bash
# 서비스 파일 복사
sudo cp deployment/alphatrader.service /etc/systemd/system/

# 서비스 파일 수정 (경로 확인)
sudo nano /etc/systemd/system/alphatrader.service
# User와 WorkingDirectory를 실제 경로로 수정

# 서비스 활성화 및 시작
sudo systemctl daemon-reload
sudo systemctl enable alphatrader
sudo systemctl start alphatrader

# 상태 확인
sudo systemctl status alphatrader
```

### 3. 수동 실행 (테스트용)

```bash
# 백그라운드에서 모니터 실행
nohup ./auto_restart_bot.sh > database/monitor.log 2>&1 &
```

## 📊 모니터링

### 로그 확인

```bash
# 봇 로그
tail -f database/trading_$(date +%Y%m%d).log

# 재시작 로그
tail -f database/restart.log

# 서비스 로그
sudo journalctl -u alphatrader -f
```

### 프로세스 확인

```bash
# 봇 프로세스 확인
pgrep -f "run_bot.py"

# 대시보드 프로세스 확인
pgrep -f "streamlit"
```

## 🔧 문제 해결

### 봇이 계속 중지되는 경우

1. 로그에서 에러 확인:
```bash
grep -E "ERROR|CRITICAL" database/trading_*.log | tail -20
```

2. 일반적인 원인:
   - `APBK1680`: ETF 교육 미이수 → KIS 앱에서 교육 이수 필요
   - `APBK1681`: 기본예탁금 미충족 → 계좌에 최소 입금 필요
   - `Token Error`: API 키 만료 → 토큰 재발급 필요

### 대시보드 접속 불가

1. 방화벽 확인:
```bash
sudo iptables -L -n | grep 8501
```

2. Cloudflare Tunnel 상태 확인:
```bash
./cloudflared tunnel info
```

## 📈 시스템 개선 사항

### v2.0 업데이트 내용

1. **자동 재시작**: 봇이 크래시되면 30초 내 자동 재시작
2. **동적 수량 계산**: 가용 자금의 20%씩 각 종목에 투자
3. **재시도 제한**: 종목별 최대 3회까지만 재시도
4. **계정 제한 감지**: ETF 교육/예탁금 에러 시 해당 종목 영구 스킵
5. **Analytics 탭**: 에러 요약, 거래 통계, 티커별 분석 추가

### 설정 옵션 (user_config.json)

```json
{
    "trading_mode": "safe",      // safe(1x ETF) 또는 risky(3x ETF)
    "strategy": "swing",         // day(당일 청산) 또는 swing(추세 추종)
    "max_position_pct": 0.2,     // 종목당 최대 투자 비율 (20%)
    "max_retries_per_ticker": 3  // 종목별 최대 재시도 횟수
}
```
