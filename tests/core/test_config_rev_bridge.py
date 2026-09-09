"""설정 변경 신호의 프로세스 간 전파 (core/config_rev_bridge.py).

⚠️ 2026-08-31 신설 — API 게이트웨이 Phase 2로 road-service가 별도
프로세스가 되면서, platform-shell의 ``config_rev.bump()``가 road-service
안의 워처를 더는 즉시 깨우지 못하게 됐다(최대 60초 폴링 지연으로
후퇴). 이 시험은 실제 PostgreSQL LISTEN/NOTIFY를 통해 — 같은
프로세스 안이지만 서로 다른 커넥션으로 — [[notify]]가 [[listen_loop]]를
실제로 즉시 깨우는지 확인한다(단위 시험이 아니라 통합 시험 — DB가
없으면 건너뛴다).
"""
from __future__ import annotations

import threading
import time

import pytest

from tot_dashboard.core import config_rev as CR
from tot_dashboard.core import config_rev_bridge as BR


@pytest.fixture(autouse=True)
def clean():
    CR.reset()
    yield
    CR.reset()


@pytest.fixture()
def listener(db_schema):
    """LISTEN 스레드를 띄우고, 시험이 끝나면 반드시 멈춘다."""
    stop = threading.Event()
    th = threading.Thread(target=BR.listen_loop, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.5)  # LISTEN 등록이 실제로 걸릴 시간을 준다
    yield stop
    stop.set()
    th.join(timeout=3.0)
    assert not th.is_alive(), "listen_loop 이 종료 신호를 받고도 멈추지 않았다"


def test_notify가_다른_프로세스_취급인_리스너를_즉시_깨운다(listener):
    """다른 프로세스 대신 별도 스레드/커넥션으로 흉내낸다 — LISTEN/NOTIFY
    자체는 프로세스 경계와 무관하게 커넥션 단위로 동작하므로 유효한 검증이다."""
    before = CR.revision()
    t0 = time.time()
    BR.notify()
    rev = CR.wait_change_or_stop(before, 5.0, threading.Event())
    assert rev != before, "NOTIFY가 로컬 config_rev.bump()로 전파되지 않았다"
    assert time.time() - t0 < 3.0, "즉시 반영이 아니라 폴링 수준으로 느렸다"


def test_DB가_잠깐_안되도_notify는_예외를_내지_않는다(monkeypatch):
    """저장 API(bump 호출부)를 절대 500으로 만들면 안 된다 — 최선 노력이다."""
    def _boom(*a, **kw):
        raise RuntimeError("DB 연결 안 됨(시험용)")

    monkeypatch.setattr(BR.psycopg, "connect", _boom)
    BR.notify()  # 예외 없이 조용히 넘어가야 한다
