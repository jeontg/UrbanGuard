"""스트림 재접속 방어 — 네트워크가 끊겨도 서비스가 죽지 않게 한다.

무슨 일이 있었나 (2026-08-12 밤)
    부산 교통 CCTV 8개의 호스트 이름이 한동안 풀리지 않았다(DNS 장애).
    그동안 각 카메라 스레드가 **1초마다 무한히 재접속**했고, 그때마다
    OpenCV 가 FFmpeg 컨텍스트를 새로 만들었다. 초당 8개씩 몇 시간이 쌓인 끝에
    프로세스가 **세그폴트(exit 139)** 로 통째로 죽었다.

    관제 시스템이 네트워크 블립에 죽으면 침수·인파 탐지까지 함께 멎는다.

무엇을 고치는가
    **호스트가 풀리지 않으면 FFmpeg 를 아예 부르지 않는다.** DNS 조회는
    파이썬 표준 라이브러리로 할 수 있고, 실패하면 네이티브 코드 경로에
    들어가지 않으므로 크래시의 방아쇠 자체가 사라진다.

    그리고 **재접속 간격을 지수적으로 늘린다.** 끊긴 스트림에 1초마다 달려드는
    것은 복구에 도움이 되지 않으면서 자원만 태운다.

⚠️ 이것만으로 충분하지 않다
    세그폴트는 파이썬에서 잡을 수 없다. 여기서 하는 일은 **일어날 확률을 크게
    낮추는 것**이지 없애는 것이 아니다. 그래서 ``scripts/serve.py`` 감시
    프로세스를 함께 둔다 — 그래도 죽으면 되살린다.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("urbanguard.stream")

# 재접속 간격. 1초에서 시작해 두 배씩 늘리고 60초에서 멈춘다.
BACKOFF_START_SEC = 1.0
BACKOFF_MAX_SEC = 60.0
# DNS 결과 캐시. 8개 스레드가 같은 호스트를 동시에 조회하는 것을 막는다.
DNS_CACHE_SEC = 20.0
DNS_TIMEOUT_SEC = 3.0

_lock = threading.Lock()
_dns_cache: dict[str, tuple[float, bool]] = {}   # host -> (검사 시각, 성공 여부)


def host_of(url: str) -> str:
    """URL 에서 호스트만 뽑는다. 파일 경로면 빈 문자열."""
    if not url or "://" not in str(url):
        return ""
    try:
        return urlparse(str(url)).hostname or ""
    except ValueError:
        return ""


def clear_cache() -> None:
    with _lock:
        _dns_cache.clear()


def host_reachable(url: str) -> bool:
    """이 URL 의 호스트 이름이 지금 풀리는가.

    파일 경로나 호스트를 못 읽는 URL 은 **참으로 본다** — 판단할 근거가 없으면
    막지 않는다. 막아 버리면 로컬 동영상까지 재생되지 않는다.
    """
    host = host_of(url)
    if not host:
        return True

    now = time.monotonic()
    with _lock:
        hit = _dns_cache.get(host)
        if hit and (now - hit[0]) < DNS_CACHE_SEC:
            return hit[1]

    prev = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(DNS_TIMEOUT_SEC)
        socket.getaddrinfo(host, None)
        ok = True
    except (socket.gaierror, OSError):
        ok = False
    finally:
        socket.setdefaulttimeout(prev)

    with _lock:
        _dns_cache[host] = (now, ok)
    if not ok:
        log.warning("호스트 이름이 풀리지 않습니다 — 접속을 건너뜁니다: %s", host)
    return ok


class Backoff:
    """실패가 이어질수록 재시도 간격을 늘린다.

    성공하면 곧바로 처음 간격으로 되돌린다 — 잠깐 끊겼다 돌아온 스트림을
    1분씩 기다리게 하면 안 된다.
    """

    def __init__(self, start_sec: float = BACKOFF_START_SEC,
                 max_sec: float = BACKOFF_MAX_SEC, factor: float = 2.0):
        self.start = max(start_sec, 0.1)
        self.max = max(max_sec, self.start)
        self.factor = max(factor, 1.1)
        self._cur = self.start
        self.failures = 0

    @property
    def delay(self) -> float:
        return self._cur

    def fail(self) -> float:
        """이번 시도가 실패했다. 다음에 기다릴 시간을 돌려준다."""
        wait = self._cur
        self.failures += 1
        self._cur = min(self._cur * self.factor, self.max)
        return wait

    def success(self) -> None:
        self._cur = self.start
        self.failures = 0

    def sleep(self, stop_event: threading.Event | None = None) -> None:
        """다음 시도까지 기다린다. 종료 신호가 오면 즉시 멈춘다."""
        wait = self.fail()
        if stop_event is not None:
            stop_event.wait(wait)
        else:
            time.sleep(wait)
