"""설정 변경 신호 (core/config_rev.py).

폴링이 아니라 신호를 쓰는 이유는 **자고 있는 워처를 곧바로 깨우기** 위해서다.
노면 순회는 15분 주기라, 잠에서 깰 때 DB를 다시 읽게만 하면 최악의 경우
15분을 기다려야 한다. 그건 「실시간 반영」이 아니다.
"""
from __future__ import annotations

import threading
import time

import pytest

from tot_dashboard.core import config_rev as CR


@pytest.fixture(autouse=True)
def clean():
    CR.reset()
    yield
    CR.reset()


def test_저장하면_리비전이_오른다():
    before = CR.revision()
    assert CR.bump() == before + 1
    assert CR.revision() == before + 1


def test_이미_바뀌었으면_기다리지_않는다():
    """워처가 분석하는 동안 온 변경을 놓치면 안 된다."""
    CR.bump()
    t0 = time.time()
    assert CR.wait_change(0, timeout=5.0) == 1
    assert time.time() - t0 < 0.5, "이미 바뀐 값인데 기다렸다"


def test_변경되면_즉시_깨어난다():
    woke = {}

    def waiter():
        t0 = time.time()
        woke["rev"] = CR.wait_change(0, timeout=10.0)
        woke["sec"] = time.time() - t0

    th = threading.Thread(target=waiter, daemon=True)
    th.start()
    time.sleep(0.2)
    CR.bump()
    th.join(timeout=5)
    assert woke["rev"] == 1
    # 15분 주기를 기다리지 않는다는 것이 이 기능의 전부다.
    assert woke["sec"] < 2.0, f"깨어나는 데 {woke['sec']:.1f}초 걸렸다"


def test_변경이_없으면_시간_만료로_같은_값을_돌려준다():
    assert CR.wait_change(0, timeout=0.2) == 0


def test_종료_신호가_오면_기다림을_그만둔다():
    """이게 없으면 서비스 종료가 순회 주기만큼 늦어진다."""
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    t0 = time.time()
    CR.wait_change_or_stop(0, timeout=30.0, stop_event=stop, slice_sec=0.1)
    assert time.time() - t0 < 3.0


def test_대기_중_변경도_종료보다_먼저_잡힌다():
    stop = threading.Event()
    out = {}

    def waiter():
        out["rev"] = CR.wait_change_or_stop(0, timeout=10.0, stop_event=stop,
                                            slice_sec=0.1)

    th = threading.Thread(target=waiter, daemon=True)
    th.start()
    time.sleep(0.2)
    CR.bump()
    th.join(timeout=5)
    assert out["rev"] == 1
