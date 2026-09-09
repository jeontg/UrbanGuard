"""설정 변경 신호 — 상시 탐지 워처를 재시작 없이 갈아끼우기 위한 장치.

S-80에서 「도로 노면 관리 / 상시」를 켜도 **서비스를 다시 띄우기 전에는 아무
일도 일어나지 않았다.** 화면에는 「상시」로 뜨는데 실제로는 아무도 그 지점을
보고 있지 않은 상태였다 — 설정과 동작이 어긋나면 운영자는 설정을 믿을 수
없게 된다.

폴링이 아니라 신호를 쓰는 이유
    워처가 15분 주기로 자고 있으므로, 잠에서 깰 때 DB를 다시 읽게만 하면
    최악의 경우 15분을 기다려야 한다. 그건 「실시간 반영」이 아니다. 저장하는
    쪽이 리비전을 올리고 **자고 있는 워처를 곧바로 깨운다.**

한 개의 리비전으로 충분한가
    카메라 등록·수정·삭제·도메인 변경·ROI 저장이 모두 같은 대상(카메라 설정)을
    건드린다. 이들을 나눠 봐야 워처는 어차피 목록 전체를 다시 읽으므로,
    잘게 쪼개면 코드만 늘고 얻는 것이 없다.
"""
from __future__ import annotations

import threading

_cond = threading.Condition()
_rev = 0


def revision() -> int:
    """현재 설정 리비전. 워처는 이 값이 바뀌면 목록을 다시 읽는다."""
    with _cond:
        return _rev


def bump() -> int:
    """설정이 바뀌었음을 알리고 대기 중인 워처를 모두 깨운다."""
    global _rev
    with _cond:
        _rev += 1
        _cond.notify_all()
        return _rev


def wait_change(known_rev: int, timeout: float) -> int:
    """``known_rev`` 에서 바뀔 때까지 최대 ``timeout`` 초 기다린다.

    바뀌었으면(또는 이미 달랐으면) 새 리비전을, 시간이 다 됐으면 같은 값을
    돌려준다. 호출자는 값이 달라졌는지로 판단한다.

    ⚠️ 종료 신호(``stop_event``)와 함께 쓰려면 ``wait_change_or_stop`` 을
    써야 한다 — 여기서만 기다리면 서비스 종료 시 최대 ``timeout`` 만큼
    프로세스가 내려가지 않는다.
    """
    with _cond:
        if _rev != known_rev:
            return _rev
        _cond.wait(timeout=max(timeout, 0.0))
        return _rev


def wait_change_or_stop(known_rev: int, timeout: float,
                        stop_event: threading.Event,
                        slice_sec: float = 1.0) -> int:
    """설정 변경 · 종료 · 시간 만료 중 먼저 오는 것을 기다린다.

    ``threading.Event`` 와 ``Condition`` 을 한 번에 기다릴 방법이 없어 짧게
    끊어 번갈아 확인한다. 1초 조각은 종료 지연으로 체감되지 않으면서
    깨어나는 비용도 무시할 만하다.
    """
    remaining = max(timeout, 0.0)
    while remaining > 0 and not stop_event.is_set():
        step = min(slice_sec, remaining)
        rev = wait_change(known_rev, step)
        if rev != known_rev:
            return rev
        remaining -= step
    return revision() if stop_event.is_set() else known_rev


def reset() -> None:
    """테스트용 — 리비전을 0으로 되돌린다."""
    global _rev
    with _cond:
        _rev = 0
