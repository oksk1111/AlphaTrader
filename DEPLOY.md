# ☁️ Alpha Trader Cloud Installation Guide

이 가이드는 **Oracle Cloud (Ubuntu)** 또는 **GCP (e2-micro)** 환경에서 봇을 구동하기 위한 절차입니다.

## 1. 서버 준비 (Oracle Cloud 추천)
1. **인스턴스 생성**: Ubuntu 22.04 또는 24.04 LTS 이미지 선택.
2. **포트 개방 (Ingress Rules)**:
   - SSH: `22` (기본)
   - Streamlit Dashboard: `8501`

## 2. 설치 (터미널 접속 후)

```bash
# 1. 깃허브 클론
git clone https://github.com/oksk1111/AlphaTrader.git
cd AlphaTrader

# 2. 셋업 스크립트 실행 실행
chmod +x deployment/setup_cloud.sh
./deployment/setup_cloud.sh
```

## 3. 설정 (.env)
설치 스크립트가 실행되면 프로젝트 폴더에 `.env` 파일이 생성됩니다.
`nano .env` 명령어로 파일을 열어 API 키를 입력하세요.

```bash
nano .env
```
(입력 후 `Ctrl+X`, `Y`, `Enter`로 저장)

## 4. 실행 및 자동실행 등록
스크립트가 `systemd` 서비스를 이미 생성했습니다. 다음 명령어로 봇과 대시보드를 켭니다.

```bash
# 봇 시작
sudo systemctl enable alphatrader-bot
sudo systemctl start alphatrader-bot

# 대시보드 시작
sudo systemctl enable alphatrader-dashboard
sudo systemctl start alphatrader-dashboard
```

## 5. 상태 확인

```bash
# 봇 상태 확인
sudo systemctl status alphatrader-bot

# 대시보드 상태 확인
sudo systemctl status alphatrader-dashboard

# 로그 실시간 확인
journalctl -u alphatrader-bot -f
```

## 6. 대시보드 접속
브라우저 주소창에 `http://<서버_공인_IP>:8501` 입력.
