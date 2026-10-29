# 배포 · 운영 가이드

Oracle Cloud(Ubuntu) 한 대에서 봇(`run_bot.py`), 대시보드(FastAPI `web.app`, 포트 8501),
토스 실시간 수집기(`modules.shadow_collector`, 주문 안 함)를 돌린다.
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
| `TOSS_ACCOUNT_SEQ` | 선택 | 계좌 식별값. 비우면 종합매매(BROKERAGE) 계좌가 하나일 때 자동 선택 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 권장 | 알림 |
| `LOGIN_PASSWORD` | 선택 | 비상용 대시보드 비밀번호 로그인. Google OAuth 운영 시 비워 두는 것을 권장 |
| `DASHBOARD_API_KEY` | 선택 | 비상용 API key. URL 공유 금지 — OAuth 운영에서는 사용하지 않는 것을 권장 |
| `JWT_SECRET` | ✅ (OAuth) | 32바이트 이상 무작위 세션 서명키. 재배포 뒤에도 세션을 검증한다 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | ✅ (OAuth) | Google Cloud OAuth Web application 자격증명 |

### GitHub Variables (같은 화면의 Variables 탭)

| 이름 | 값 | 의미 |
|------|----|------|
| `EXECUTION_MODE` | `shadow` (미설정 시 기본) | 판단·로그·알림은 전부 돌고 **주문만 보내지 않는다**. 알림에 `SHADOW` 표시 |
| | `live` | **실주문.** 토스는 공식 모의투자 서버가 없다 — shadow 로 며칠 확인한 뒤 전환할 것 |
| `OAUTH_REDIRECT_URI` | `https://<dashboard-domain>/auth/callback` | Google Cloud Console에 같은 URI를 등록해야 한다 |
| `ALLOWED_EMAILS` | `operator@example.com` | 쉼표 구분 OAuth 이메일 allowlist. **빈 값이면 OAuth 로그인을 전부 거부** |
| `DASHBOARD_COOKIE_SECURE` | `true` | HTTPS에서만 쿠키를 전송한다. 로컬 HTTP 개발 외에는 변경하지 않는다 |

`EXECUTION_MODE` 는 시크릿이 아니라 변수다(값을 로그에서 확인할 수 있어야 한다). 배포 로그에
`ℹ️ EXECUTION_MODE=...` 줄이 찍힌다. 되돌리려면 변수를 `shadow` 로 바꾸고 재배포(Actions → Run workflow).

### Live-order release gates (필수)

`EXECUTION_MODE=live`는 토스 주문 API **전송 능력**일 뿐 정상 전략 주문의 허가가 아니다. 다음이 모두
통과하지 않으면 `TossBroker.submit_order()`가 HTTP 요청 전에 주문을 거부한다.

1. 코드 변경으로 전략/유니버스/리밸런싱/방어자산을 건드렸다면 배포 전 `python scripts/backtest_real.py --robustness`
   결과를 검토한다. 외부 시세 공급자 가용성 때문에 일반 CI가 아니라 변경 승인 체크리스트에서 관리한다.
2. GitHub Environment `production-live`의 required reviewer 승인. `.github/workflows/deploy.yml`의 live deploy는
   이 Environment에 연결된다. GitHub Settings → Environments에서 reviewer를 설정한다.
3. GitHub Secret `LIVE_GATE_SECRET` 등록 — `JWT_SECRET`처럼 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
   로 생성하는 32바이트 이상 비밀값이다. preflight/smoke/release 기록의 위변조를 막는다. 이 키가 없으면
   **shadow 배포도 중단**한다. shadow 세션을 live 증적으로 쓰려면 같은 signed preflight가 필요하기 때문이다.
4. 배포 중 **read-only preflight** 성공. `scripts/live_preflight.py`는 shadow broker로 인증·계좌 선택·KRW/USD
   매수가능금액·KR/US 현재가·웹소켓 구독을 확인하며 주문 API는 호출하지 않는다. 실패하면 bot을 기동하지 않는다.
5. 각 시장별로 `execution_mode=shadow preflight=ok`가 기록된 완료 로테이션 세션 **20회**. 이전 세션은
   provenance가 없으므로 live 증적으로 세지 않는다.
6. 운영자 승인 one-share smoke test가 각 시장에서 체결·잔고 재동기화까지 성공. CI나 bot은 절대 임의 주문하지 않는다.
7. 운영자가 최대 24시간짜리 signed release를 명시적으로 생성. 만료 뒤 다음 live 주문은 다시 차단된다.

운영 명령(서버에서, 각 단계의 JSON 결과가 `ok`인지 확인):

```bash
# 1) 읽기 전용 preflight (live 배포 시 자동 실행되지만 수동 재검증 가능)
venv/bin/python scripts/live_preflight.py

# 2) 20-session 증적 + preflight/smoke 상태 확인
venv/bin/python scripts/live_gate_status.py --required-shadow-sessions 20

# 3) 1주 smoke test는 두 단계이며, 운영자만 의도적으로 실행한다.
# 예: US XLK 1주를 $300 상한으로 승인 후 실제 주문/체결/잔고 재동기화 검증
venv/bin/python scripts/live_smoke.py authorize --market US --symbol XLK --max-notional 300
venv/bin/python scripts/live_smoke.py execute --market US --symbol XLK --max-notional 300 \
  --confirm I_UNDERSTAND_THIS_SENDS_A_REAL_ORDER

# 4) KR/US smoke와 모든 증적이 충족된 뒤 24시간 정상 주문 릴리스
venv/bin/python scripts/authorize_live_release.py --commit "$(grep '^DEPLOYED_COMMIT=' .env | cut -d= -f2)" \
  --required-shadow-sessions 20 --confirm I_AUTHORIZE_NORMAL_LIVE_ORDERS_FOR_24_HOURS
```

> smoke test는 실제 보유와 수수료를 만들며 자동 매도가 아니다. `--max-notional`은 승인 상한이고
> 실행할 종목/시장/보유·매도 계획을 운영자가 결정한다. 체결되지 않은 주문은 어댑터가 timeout 뒤 취소한다.

- 시크릿은 **값이 있을 때만** 서버 `.env` 의 해당 줄을 덮어쓴다. 등록 전에 배포가 돌아도 기존 값이 지워지지 않는다.
- 배포 시 서버 `.env` 의 `KIS_*` · `BROKER` · `MARKET_DATA_BROKER` 줄은 삭제된다 (v8.0 에서 한국투자증권 API 제거).
- `GEMINI_API_KEY`, `DASHBOARD_API_KEY` 등 나머지 키는 서버 `.env` 에 직접 둔다 (`.env.example` 참고).

### 토스증권 쪽 준비 (GitHub 밖)

1. 토스증권 WTS → 설정 → Open API 에서 `client_id` / `client_secret` 발급
2. 같은 화면 **허용 IP 관리**에 서버 공인 IP 등록 — 미등록이면 모든 호출이 `403 ip-not-allowed`
3. US 를 운용하려면 앱에서 **달러로 환전**해 둔다 (Open API 주문은 자동환전이 없다)
4. 레버리지·인버스 등 사전 요건이 필요한 종목은 앱에서 해당 종목 구매 화면의 등록 절차를 먼저 마친다
   (`422 prerequisite-required`)

## 2. HTTPS + Google OAuth 대시보드 (운영 권장)

포트 `8501`을 공인 인터넷에 직접 열어 두면 HTTP 구간에서 로그인 비밀번호와 세션이 노출될 수 있다.
운영 대시보드는 도메인과 TLS reverse proxy(Nginx 또는 Caddy) 뒤에 두고, Uvicorn은 loopback에만 바인딩한다.

> **자동 구성 (2026-10-29~).** 배포 때 `deployment/setup_https.sh` 가 Caddy 를 설치하고
> `DASHBOARD_DOMAIN → 127.0.0.1:8501` 프록시, Let's Encrypt 인증서, 서버 iptables 80/443 을 맞춘다.
> `DASHBOARD_DOMAIN`(Variables) 이 비어 있으면 `158-180-81-25.sslip.io` 처럼 공인 IP 기반 도메인을 쓰고,
> `OAUTH_REDIRECT_URI` 가 비어 있으면 `https://<도메인>/auth/callback` 으로 채운다. 실패해도 봇 배포는 막지 않는다.
> 사람이 해야 하는 것은 두 가지뿐이다: **(a)** Oracle Cloud VCN Security List 에 80·443 ingress 추가,
> **(b)** Google Cloud Console OAuth client 의 Authorized redirect URI 에 같은 주소 등록.
> `ALLOWED_EMAILS` 등은 Variables·Secrets 어느 쪽에 넣어도 읽는다. 아래 1~5 는 수동 구성 참고용이다.

1. DNS A 레코드 `dashboard.example.com`을 서버 공인 IP로 연결한다.
2. Google Cloud Console → OAuth consent screen / Credentials에서 **Web application** OAuth client를 만든다.
   Authorized redirect URI에 `https://dashboard.example.com/auth/callback`을 정확히 등록한다.
3. GitHub Secrets에 `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `JWT_SECRET`을 넣고, Variables에
   `OAUTH_REDIRECT_URI=https://dashboard.example.com/auth/callback`,
   `ALLOWED_EMAILS=<운영자 이메일>`, `DASHBOARD_COOKIE_SECURE=true`를 넣는다.
4. 보안 그룹/방화벽은 `80/tcp`, `443/tcp`만 공개하고 **`8501/tcp` 공개 ingress를 제거**한다.
5. 아래 프록시처럼 `127.0.0.1:8501`로만 전달한다. 인증된 대시보드에서 봇 로그와 상태를 볼 수 있으며,
   `/api/status`의 비인증 응답은 의도적으로 최소 health 정보만 노출한다.

```nginx
server {
    listen 80;
    server_name dashboard.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name dashboard.example.com;
    # certbot 또는 조직의 인증서 관리 방식으로 설정
    ssl_certificate /etc/letsencrypt/live/dashboard.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dashboard.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
    }
}
```

> `DASHBOARD_API_KEY`의 `?key=` URL 방식은 브라우저 기록·프록시 로그에 남을 수 있다.
> 운영 접근에는 Google OAuth allowlist를 사용하고 API key는 비상 대응용으로만 보관한다.

## 3. 서버 최초 설치

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

대시보드: 위 HTTPS reverse proxy를 구성했다면 `https://dashboard.example.com`으로 접속한다.
운영 점검을 위한 SSH 터널은 `ssh -i <키파일>.key -N -L 8501:localhost:8501 ubuntu@<서버_IP>` 후
`http://localhost:8501`을 사용한다. 공인 방화벽/Ingress에서 `8501/tcp`는 열지 않는다.

## 4. 상태 확인

```bash
curl -s http://<서버_IP>:8501/api/status    # bot_status · market_status · heartbeat_age_sec · session_active · session_outcome
tail -f database/trading.log               # 봇 로그 ([bot] 태그). 대시보드 로그는 database/dashboard.log
tail -f database/deploy.log                # 배포 기록
```

- 장중인데 `session_active: false` 면 먼저 `session_outcome` 을 본다 (AGENTS v7.0.1 규칙 8).
- 로그의 `[TOSS-KR]`/`[TOSS-US]` 는 주문·잔고(체결 상태 포함), `Toss WS` 는 웹소켓 재연결이다.
- 수집기 상태: `database/toss_stream_status.json` (연결 · 메시지 나이 · 수집 세션 수). 로그 `database/toss_shadow_stdout.log`.
- 토스 웹소켓은 **계정당 동시 연결 2개**다 — 수집기 1 + 봇 세션 1. 로컬에서 하나 더 열면 가장 오래된
  연결(수집기)이 끊긴다.

## 5. 문제 해결 (토스 에러 코드)

| 증상 / 코드 | 원인 | 조치 |
|-------------|------|------|
| `403 ip-not-allowed` | 서버 IP 미등록 / IP 변경 | WTS 허용 IP 에 현재 공인 IP 등록 |
| `401 token-revoked` 반복 | 다른 곳(로컬 PC 등)에서 같은 클라이언트로 토큰 발급 | 클라이언트당 토큰 1개. 다른 곳의 실행을 멈춘다 |
| `missing-credentials` | `.env` 에 `TOSS_CLIENT_ID/SECRET` 없음 | GitHub Secrets 등록 후 재배포 |
| `422 insufficient-buying-power` (US) | USD 부족 — 자동환전 없음 | 앱에서 환전 |
| `422 prerequisite-required` / `account-restricted` | 종목 사전 요건·계좌 제한 | 앱에서 절차 완료. 봇은 해당 종목 매매를 멈춘다 |
| `422 order-hours-closed` | 주문 불가 시간 | 정상 — 다음 세션에 재시도 |
| `409 opposite-pending-order-exists` (KR) | 같은 종목 반대 방향 미체결 | 미체결 취소 후 재시도 |
| `429 rate-limit-exceeded` | 초당 한도 초과 | 호출을 줄이고 다음 세션에서 재시도. 현재 어댑터는 429를 즉시 오류로 올려 원인을 숨기지 않는다 |

## 6. 로컬 실행 (개발)

```bash
pip install -r requirements.txt
python run_bot.py                                            # 봇
python -m uvicorn web.app:app --host 127.0.0.1 --port 8501  # 대시보드
```

로컬에서 토스 API 를 부르려면 로컬 공인 IP 도 허용 IP 에 등록해야 한다. 그리고 로컬에서 토큰을
발급하면 **서버 봇의 토큰이 무효화된다** — 운영 중에는 로컬에서 토스 API 를 호출하지 말 것.
