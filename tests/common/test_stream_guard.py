"""스트림 재접속 방어 (common/stream_guard.py).

2026-08-12 밤, 부산 CCTV 호스트가 풀리지 않는 동안 8개 스레드가 1초마다
재접속하면서 FFmpeg 컨텍스트를 초당 8개씩 만들었고 프로세스가 세그폴트로
죽었다. 여기서 검증하는 것은 **그 방아쇠를 당기지 않는가**다.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from tot_dashboard.common import stream_guard as SG


@pytest.fixture(autouse=True)
def clean():
    SG.clear_cache()
    yield
    SG.clear_cache()


# --- 호스트 판정 -------------------------------------------------------------

def test_URL에서_호스트를_뽑는다():
    assert SG.host_of("https://its-stream3.busan.go.kr:8443/x.m3u8") \
        == "its-stream3.busan.go.kr"
    assert SG.host_of("rtsp://10.0.0.5:554/live") == "10.0.0.5"


def test_파일_경로는_호스트가_없다():
    assert SG.host_of(r"D:\dev-PoC\UrbanGuard\data\videos\a.mp4") == ""
    assert SG.host_of("") == ""


def test_파일_경로는_막지_않는다():
    """판단할 근거가 없으면 막지 않는다 — 막으면 로컬 동영상까지 안 나온다."""
    assert SG.host_reachable("data/videos/a.mp4") is True
    assert SG.host_reachable("") is True


def test_풀리지_않는_호스트는_거른다(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("이름을 확인할 수 없습니다")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert SG.host_reachable("https://없는호스트.example/x.m3u8") is False


def test_풀리는_호스트는_통과시킨다(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [("x",)])
    assert SG.host_reachable("https://ok.example/x.m3u8") is True


def test_결과를_캐시해_DNS를_난타하지_않는다(monkeypatch):
    """카메라 8개가 같은 호스트를 동시에 조회하는 것을 막는다."""
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (calls.append(a), [("x",)])[1])
    for _ in range(10):
        SG.host_reachable("https://ok.example/a.m3u8")
    assert len(calls) == 1


def test_호스트가_다르면_따로_조회한다(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (calls.append(a[0]), [("x",)])[1])
    SG.host_reachable("https://a.example/x")
    SG.host_reachable("https://b.example/x")
    assert calls == ["a.example", "b.example"]


def test_조회가_터져도_예외를_올리지_않는다(monkeypatch):
    def _boom(*a, **k):
        raise OSError("소켓 오류")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert SG.host_reachable("https://x.example/a") is False


# --- 백오프 -----------------------------------------------------------------

def test_실패가_이어지면_간격이_늘어난다():
    """1초마다 무한 재접속한 것이 세그폴트의 원인이었다."""
    b = SG.Backoff(start_sec=1.0, max_sec=60.0, factor=2.0)
    waits = [b.fail() for _ in range(5)]
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_간격에는_상한이_있다():
    b = SG.Backoff(start_sec=1.0, max_sec=10.0)
    for _ in range(20):
        b.fail()
    assert b.delay == 10.0


def test_성공하면_곧바로_처음_간격으로_되돌린다():
    """잠깐 끊겼다 돌아온 스트림을 1분씩 기다리게 하면 안 된다."""
    b = SG.Backoff(start_sec=1.0)
    for _ in range(6):
        b.fail()
    assert b.delay > 1.0
    b.success()
    assert b.delay == 1.0
    assert b.failures == 0


def test_실패_횟수를_센다():
    b = SG.Backoff()
    for _ in range(3):
        b.fail()
    assert b.failures == 3


def test_종료_신호가_오면_기다림을_그만둔다():
    """이게 없으면 서비스 종료가 백오프 간격만큼 늦어진다."""
    b = SG.Backoff(start_sec=30.0)
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    t0 = time.monotonic()
    b.sleep(ev)
    assert time.monotonic() - t0 < 3.0
