"""
logger.py — 공용 로거

[v6.1 에서 이 파일이 바뀐 이유 — "몇 달째 거래가 안 된다"의 관측 실패 원인]

v6.0 까지 이 파일은 이렇게 돼 있었다:

    file_handler = logging.FileHandler(
        f"database/trading_{datetime.now().strftime('%Y%m%d')}.log")

`logging.FileHandler` 는 **프로세스가 시작될 때 파일명을 한 번 확정**하고 그 뒤로는
절대 바꾸지 않는다. 날짜 회전이 없다. 그래서:

  · 9월 8일 배포로 뜬 봇 프로세스는 그 뒤로 며칠이 지나든 계속
    `database/trading_20260908.log` 에만 쓴다.
  · 대시보드(uvicorn)는 별도 프로세스다. 나중에 재시작되면 그 시점 날짜로
    `database/trading_20260910.log` 를 새로 만들고, 거기에는 대시보드가 내는
    계좌 캐시 갱신 로그만 쌓인다.
  · 그런데 대시보드의 `get_latest_log_file()` 은 `sorted(...)[-1]`, 즉
    **파일명이 가장 큰(=가장 최근 날짜) 파일**을 고른다.

결과: 대시보드 화면에는 대시보드 자신이 쓴 "✅ Account Cache Updated" 만 뜨고,
봇이 쓴 매매 로그는 **단 한 줄도 표시되지 않는다.** 사용자는 "봇이 아무것도 안
한다"고 볼 수밖에 없고, 실제로 봇이 무엇을 하고 있었는지는 아무도 알 수 없었다.

이 프로젝트가 v3 → v6 까지 네 번이나 "전면 개편"을 반복하면서도 매번 원인을
확신하지 못한 진짜 이유가 이것이다. 전략을 고친 게 아니라 **계기판이 고장난 채로
계속 엔진만 뜯어고쳤다.**

[v6.1 의 해법]

  1. 파일명을 고정(`database/trading.log`)하고 회전은 핸들러에 맡긴다.
     `TimedRotatingFileHandler(when='midnight')` 는 자정에 스스로
     `trading.log.2026-09-10` 으로 넘기고 새 파일을 연다. 프로세스가 몇 달을
     살아 있어도 로그가 오늘 파일에 쌓인다.
  2. 파일 소유자를 이름으로 못박는다. 봇은 `trading.log`, 대시보드는
     `dashboard.log`. 어느 프로세스가 언제 재시작되든 봇 로그는 항상
     `trading.log` 다. 대시보드 화면은 "가장 최근 파일"을 추측하지 않고
     이 경로를 직접 읽는다 (modules.logger.BOT_LOG_FILE).
  3. 로그 줄에 프로세스 이름을 남긴다(`[bot]` / `[dash]`). 어느 줄이 누구
     것인지 화면에서 바로 구분된다 — 이번 오진의 직접적 원인이었다.
  4. 핸들러 중복 등록을 막는다 (모듈이 여러 번 import 돼도 한 줄이 두 번
     찍히지 않도록).
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "database"
BACKUP_DAYS = 30

os.makedirs(LOG_DIR, exist_ok=True)


def _proc_tag() -> str:
    """이 프로세스가 봇인지 대시보드인지 한 단어로 표시."""
    argv = " ".join(sys.argv).lower()
    if "run_bot" in argv:
        return "bot"
    if "uvicorn" in argv or "web.app" in argv:
        return "dash"
    if "pytest" in argv:
        return "test"
    return os.path.basename(sys.argv[0] or "proc").replace(".py", "") or "proc"


PROC = _proc_tag()

# [v6.1] 프로세스마다 **다른 파일**에 쓴다.
#
# 한 파일에 두 프로세스가 쓰면 TimedRotatingFileHandler 의 자정 회전이 서로
# 경합한다(한쪽이 rename 한 뒤 다른 쪽은 이미 이름이 바뀐 fd 에 계속 쓴다) —
# 지금 고치려는 것과 똑같은 종류의 "조용히 엉뚱한 파일에 쓰는" 버그다.
#
# 대신 봇은 trading.log, 대시보드는 dashboard.log 로 분리하고,
# 대시보드 화면은 **명시적으로 trading.log(봇 로그)** 를 읽는다.
# 파일이 갈라지는 게 아니라, 어느 파일이 누구 것인지가 이름으로 확정된다.
_LOG_BASENAME = {
    "bot": "trading.log",
    "dash": "dashboard.log",
    "test": "test.log",
}.get(PROC, "trading.log")

LOG_FILE = os.path.join(LOG_DIR, _LOG_BASENAME)

# 대시보드가 읽어야 할 "봇 로그" 경로. 화면과 로그 파일이 어긋나지 않도록
# 경로를 코드 한 곳에서만 정의한다.
BOT_LOG_FILE = os.path.join(LOG_DIR, "trading.log")


def setup_logger():
    logger = logging.getLogger("Alphatrader")
    logger.setLevel(logging.INFO)

    # 모듈이 재import 되어도 핸들러가 중복 등록되지 않도록.
    if getattr(logger, "_alphatrader_configured", False):
        return logger

    formatter = logging.Formatter(
        f"%(asctime)s - %(levelname)s - [{PROC}] %(message)s"
    )

    # 자정(서버 TZ=Asia/Seoul)에 회전. 프로세스가 며칠을 살아 있어도
    # 오늘 로그는 항상 LOG_FILE 에 있다.
    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=BACKUP_DAYS,
        encoding="utf-8", delay=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger._alphatrader_configured = True
    return logger


logger = setup_logger()
