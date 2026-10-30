"""[v8.3] 봇·수집기·대시보드가 토스 호출 한도를 하나의 공용 카운터로 나눠 쓰는가.

2026-10-30 KR 09:10: 프로세스마다 메모리 카운터로 따로 세서, 개장 직후 봇과 수집기가 동시에
일봉을 받자 합산이 client_id 한도를 넘었다 → 379800 일봉 429 → 방어자산(국고채) 매수 보류.
"""

import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from modules.brokers import toss  # noqa: E402


class _NoopFcntl:
    """Windows 에서도 '파일이 상태를 갖는다'는 점을 검사하기 위한 대역."""
    LOCK_EX, LOCK_UN = 2, 8

    @staticmethod
    def flock(fd, op):
        return None


@pytest.fixture
def shared(monkeypatch, tmp_path):
    monkeypatch.setattr(toss, "RATE_FILE", tmp_path / "rate.json")
    if toss.fcntl is None:
        monkeypatch.setattr(toss, "fcntl", _NoopFcntl)
    monkeypatch.setattr(toss, "_RATE", {})
    return tmp_path / "rate.json"


def test_calls_recorded_by_another_process_count_against_this_one(shared):
    """다른 프로세스가 이미 1초 안에 한도를 다 썼으면, 이 프로세스는 메모리가 비어 있어도 기다린다."""
    now = time.time()
    shared.write_text(json.dumps({"/api/v1/candles": [now - 0.1] * 15}), encoding="utf-8")
    assert toss._reserve("/api/v1/candles", 15) > 0
    assert toss._reserve("/api/v1/prices", 12) == 0          # 다른 그룹은 영향 없음


def test_reservation_is_written_to_the_shared_file(shared):
    for _ in range(3):
        assert toss._reserve("/api/v1/candles", 15) == 0
    assert len(json.loads(shared.read_text(encoding="utf-8"))["/api/v1/candles"]) == 3


def test_old_and_future_stamps_do_not_block(shared):
    now = time.time()
    shared.write_text(json.dumps({"/api/v1/candles": [now - 5] * 15 + [now + 3600] * 15}),
                      encoding="utf-8")
    assert toss._reserve("/api/v1/candles", 15) == 0         # 시계가 뒤로 가도 영구 정지 없음


def test_corrupt_file_is_overwritten_not_a_permanent_fallback(shared):
    shared.write_text("{not json", encoding="utf-8")
    assert toss._reserve("/api/v1/candles", 15) == 0
    assert json.loads(shared.read_text(encoding="utf-8"))["/api/v1/candles"]


_CHILD = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from modules.brokers import toss
start = float(sys.argv[2])
while time.time() < start:
    time.sleep(0.005)
print(sum(1 for _ in range(15) if toss._reserve("/api/v1/candles", 15) == 0))
"""


@pytest.mark.skipif(toss.fcntl is None, reason="운영 서버(Linux) flock 경로 — CI 에서 검사")
def test_two_real_processes_share_one_budget(tmp_path):
    """봇과 수집기를 흉내 낸 두 프로세스가 같은 순간 15건씩 요청해도 합산 허용은 15건."""
    env = dict(os.environ, TOSS_RATE_FILE=str(tmp_path / "rate.json"))
    start = time.time() + 1.5
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, REPO, str(start)],
                              stdout=subprocess.PIPE, env=env, text=True) for _ in range(2)]
    granted = [int(p.communicate(timeout=30)[0].strip()) for p in procs]
    assert sum(granted) == 15, granted
