# Alpha Trader Cloud 접속 및 설치 가이드

## 1. SSH 접속 (로컬 터미널에서)

다운로드 받은 키 파일(`ssh-key-202X-XX-XX.key`)이 있는 폴더에서 아래 명령어를 실행하세요.
(키 파일 권한 설정이 안 되어 있으면 접속이 거부될 수 있습니다.)

```bash
# 1. 키 파일 권한 변경 (필수)
chmod 400 <다운받은_키파일_이름>.key

# 2. SSH 접속
ssh -i <다운받은_키파일_이름>.key ubuntu@158.180.81.25
```
*(주의: Oracle Linux 이미지를 선택했다면 `ubuntu` 대신 `opc`를 사용해야 합니다. 위 가이드는 Ubuntu 기준입니다.)*

## 2. 서버 초기 설정 및 설치 (서버 접속 후)

성공적으로 접속되어 `ubuntu@InstanceName:~$` 프롬프트가 보인다면, 다음 명령어를 복사해서 붙여넣으세요.

```bash
# 1. 깃허브 코드 내려받기
git clone https://github.com/oksk1111/AlphaTrader.git

# 2. 프로젝트 폴더로 이동
cd AlphaTrader

# 3. 설치 스크립트 실행 권한 부여
chmod +x deployment/setup_cloud.sh

# 4. 자동 설치 시작 (약 2~3분 소요)
./deployment/setup_cloud.sh
```

## 3. 환경 변수 설정

설치가 완료되면 `.env` 파일을 열어 API 키를 입력해야 합니다.

```bash
nano .env
```
- 방향키로 이동하며 값 입력 (마우스 클릭 안됨)
- 다 썼으면 `Ctrl` + `O` -> `Enter` (저장)
- `Ctrl` + `X` (나가기)

## 4. 봇 구동 시작

```bash
# 봇 시작
sudo systemctl start alphatrader-bot

# 대시보드 시작
sudo systemctl start alphatrader-dashboard

# 자동 실행 등록 (서버 재부팅 시 자동 켜짐)
sudo systemctl enable alphatrader-bot
sudo systemctl enable alphatrader-dashboard
```

## 5. 접속 확인

브라우저를 열고 다음 주소로 접속해 보세요:
👉 **http://158.180.81.25:8501**
