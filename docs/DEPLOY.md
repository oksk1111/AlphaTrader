# 배포 · 운영 가이드

Oracle Cloud(Ubuntu) 한 대에서 봇(`run_bot.py`)과 대시보드(FastAPI `web.app`, 포트 8501)를 돌린다.
`main` 에 푸시하면 GitHub Actions 가 검증 후 자동 배포한다.

## 1. 자동 배포 (CI/CD)

`.github/workflows/deploy.yml`

1. **verify** — 문법 검사 · `pytest tests/` · 버전별 회귀 테스트(v6.0/v6.1/v7.0/v8.0) ·
   전략 검증 루프(`verify_strategy_loop.py`, 시드 5개 × 2). 하나라도 실패하면 배포하지 않는다.
2. **deploy** — SSH 로 서버에 접속해 `git reset --hard origin/main` → GitHub Secrets 를 서버 `.env`
   에 반영 → `deployment/deploy.sh` (배포 락 · 봇/대시보드 재기동 · 기동 확인).
3. Telegram 으로 배포 성공/실패 알림.

### GitHub Secrets (Settings → Secrets and variables → Actions)

| 이름 | 필수 | 용도 |
|------|------|------|
| `ORACLE_HOST` / `ORACLE_USERNAME` / `ORACLE_SSH_KEY` | ✅ | 배포 SSH 접속 |
| `TOSS_CLIENT_ID` | ✅ | 토스증권 Open API 클라이언트 ID |
| `TOSS_CLIENT_SECRET` | ✅ | 토스증권 Open API 시크릿 |
| `TOSS_ACCOUNT_SEQ` | 선택 | 계좌 식별값. 비우면 첫 종합매매(BROKERAGE) 계좌 자동 선택 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 권장 | 알림 |

- 시크릿은 **값이 있을 때만** 서버 `.env` 의 해당 줄을 덮어쓴다. 등록 전에 배포가 돌아도 기존 값이 지워지지 않는다.
- 배포 시 서버 `.env` 의 `KIS_*` 줄은 삭제된다 (v8.0 에서 한국투자증권 API 제거).
- `GEMINI_API_KEY`, `DASHBOARD_API_KEY` 등 나머지 키는 서버 `.env` 에 직접 둔다 (`.env.example` 참고).

### 토스증권 쪽 준비 (GitHub 밖)

1. 토스증권 WTS → 설정 → Open API 에서 `client_id` / `client_secret` 발급
2. 같은 화면 **허용 IP 관리**에 서버 공인 IP 등록 — 미등록이면 모든 호출이 `403 ip-not-allowed`
3. US 를 운용하려면 앱에서 **달러로 환전**해 둔다 (Open API 주문은 자동환전이 없다)
4. 레버리지·인버스 등 사전 요건이 필요한 종목은 앱에서 해당 종목 구매 화면의 등록 절차를 먼저 마친다
   (`422 prerequisite-required`)

## 2. 서버 최초 설치

```bash
ssh -i <키파일>.key ubuntu@<서버_IP>          # Oracle Linux 이미지면 opc
git clone https://github.com/oksk1111/AlphaTrader.git && cd AlphaTrader
chmod +x deployment/setup_cloud.sh && ./deployment/setup_cloud.sh   # venv · 패키지 · .env 템플릿
nano .env                                                          # 키 입력
```

감시 스크립트(`auto_restart_bot.sh`)를 systemd 로 상시 실행한다. 봇이 죽거나 heartbeat 가 멈추면
(장중 5분 / 장외 30분) 재기동하고, 봇이 두 개면 가장 오래된 하나만 남긴다.

```bash
sudo cp deployment/alphatrader.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now alphatrader
```

대시보드: `http://<서버_IP>:8501` (방화벽/Ingress 에서 8501 개방). 외부 노출이 싫으면 SSH 터널:
`ssh -i <키파일>.key -N -L 8501:localhost:8501 ubuntu@<서버_IP>` 후 `http://localhost:8501`.

## 3. 상태 확인

```bash
curl -s http://<서버_IP>:8501/api/status    # bot_status · market_status · heartbeat_age_sec · session_active · session_outcome
tail -f database/trading.log               # 봇 로그 ([bot] 태그). 대시보드 로그는 database/dashboard.log
tail -f database/deploy.log                # 배포 기록
```

- 장중인데 `session_active: false` 면 먼저 `session_outcome` 을 본다 (AGENTS v7.0.1 규칙 8).
- 로그의 `[TOSS]` 는 REST, `[TOSS-WS]` 는 웹소켓(구독 확정·주문 이벤트·재연결)이다.

## 4. 문제 해결 (토스 에러 코드)

| 증상 / 코드 | 원인 | 조치 |
|-------------|------|------|
| `403 ip-not-allowed` | 서버 IP 미등록 / IP 변경 | WTS 허용 IP 에 현재 공인 IP 등록 |
| `401 token-revoked` 반복 | 다른 곳(로컬 PC 등)에서 같은 클라이언트로 토큰 발급 | 클라이언트당 토큰 1개. 다른 곳의 실행을 멈춘다 |
| `missing-credentials` | `.env` 에 `TOSS_CLIENT_ID/SECRET` 없음 | GitHub Secrets 등록 후 재배포 |
| `422 insufficient-buying-power` (US) | USD 부족 — 자동환전 없음 | 앱에서 환전 |
| `422 prerequisite-required` / `account-restricted` | 종목 사전 요건·계좌 제한 | 앱에서 절차 완료. 봇은 해당 종목 매매를 멈춘다 |
| `422 order-hours-closed` | 주문 불가 시간 | 정상 — 다음 세션에 재시도 |
| `409 opposite-pending-order-exists` (KR) | 같은 종목 반대 방향 미체결 | 미체결 취소 후 재시도 |
| `429 rate-limit-exceeded` | 초당 한도 초과 | 클라이언트가 `Retry-After` 만큼 대기 후 재시도 (자동) |

## 5. 로컬 실행 (개발)

```bash
pip install -r requirements.txt
python run_bot.py                                            # 봇
python -m uvicorn web.app:app --host 127.0.0.1 --port 8501  # 대시보드
```

로컬에서 토스 API 를 부르려면 로컬 공인 IP 도 허용 IP 에 등록해야 한다. 그리고 로컬에서 토큰을
발급하면 **서버 봇의 토큰이 무효화된다** — 운영 중에는 로컬에서 토스 API 를 호출하지 말 것.
