# 백테스트 자동화 운영 가이드

## 목적

실거래 히스토리(`database/asset_snapshots.json`, `database/profit_history.json`)를 기반으로
주기적인 백테스트 리포트를 자동 생성해 전략 성과를 점검합니다.

## 실행 구성

- 실행 스크립트: `modules/backtest_runner.py`
- 자동 트리거: `auto_restart_bot.sh`
- 주기: **매주 일요일 07:00 KST, 주 1회**
- 중복 방지 플래그: `database/.weekly_backtest_sent` (ISO week 기준)

## 수동 실행

```bash
source venv/bin/activate
python modules/backtest_runner.py
```

## 출력 파일

- 마크다운 리포트(이력): `database/backtest_reports/backtest_YYYYMMDD_HHMMSS.md`
- 최신 요약(JSON): `database/backtest_latest.json`
- 실행 로그(stdout): `database/backtest_stdout.log`

## 리포트 지표

- 누적 수익률 (Cumulative Return)
- 최대 낙폭 (Max Drawdown)
- 평균 일간 수익률 / 일간 변동성
- 연환산 Sharpe
- 승/패/보합 일수 및 승률
- 총 실현손익 및 거래 건수
- 30일/90일 롤링 수익률 (충분한 데이터가 있을 때)

## 장애 대응

1. 가상환경 확인

```bash
ls -al venv/bin/python
```

2. 로그 확인

```bash
tail -n 100 database/backtest_stdout.log
tail -n 100 database/restart.log
```

3. 데이터 파일 확인

```bash
ls -al database/asset_snapshots.json database/profit_history.json
```

4. 리포트 강제 재생성

```bash
rm -f database/.weekly_backtest_sent
python modules/backtest_runner.py
```

## 참고

- 백테스트 리포트는 외부 API를 호출하지 않으므로 장 외 시간에도 안전하게 실행됩니다.
- 스냅샷 데이터가 2일 미만이면 `insufficient-data` 상태 리포트를 생성합니다.