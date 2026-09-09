# -*- coding: utf-8 -*-
"""``_block_loop``의 오류 진단 출력 자체가 실패해도 스레드가 죽지 않는다
(2026-08-29 실사용 중 발견).

## 왜 이 시험이 있나

``_block_loop``의 예외 처리부는 "블록마다 첫 오류만 트레이스백을 남긴다"
(2026-08-20 신설, 54,515회 쌓인 오류를 못 찾았던 사례 참고). 그런데 그
진단 문구 자체("첫 오류 — 아래 트레이스백은...")에 **em-dash("—")** 가
들어 있어, 콘솔 코드페이지가 cp949(한국어 Windows 기본값)인 채로 이
문자를 출력하면 ``print()``가 ``UnicodeEncodeError``를 던진다. 이미
예외를 처리하던 except 블록 **안**에서 새 예외가 나면 그걸 잡아 줄
바깥 try/except가 없어, 오류를 기록하려던 시도가 오히려 **이
카메라의 감시 스레드 전체를 조용히 죽인다** — 서비스 재기동 전까지 그
카메라는 다시는 관측되지 않는다(2026-08-20에 고치려던 것보다 훨씬 나쁜
결과다). 진단 출력 자체를 한 번 더 감싸 이 연쇄를 끊는다.

(``scripts/serve.py``의 자식 프로세스 인코딩 수정으로 실제 배포
환경에서는 이 경로 자체가 더는 발생하지 않아야 하지만, 이 시험은 그
수정과 무관하게 **로그 출력이 실패하는 어떤 경우에도 스레드가 죽지
않는다**는 방어선 자체를 고정한다 — 두 겹의 방어가 원칙이다.)
"""
from __future__ import annotations

import builtins
import threading
from types import SimpleNamespace

import pytest

from tot_dashboard.core import config_rev
from tot_dashboard.service.runner import PipelineRunner

BID = "TEST-ERRLOG-SAFETY"


@pytest.fixture(autouse=True)
def _reset_rev():
    config_rev.bump()
    yield


def _make_runner() -> PipelineRunner:
    r = object.__new__(PipelineRunner)
    r._stop_ev = threading.Event()
    r.dt = 0.0
    return r


class _StopHere(RuntimeError):
    pass


def _make_ctx(runner):
    def _stop_after_river(*a, **k):
        runner._stop_ev.set()
        raise _StopHere("river_agent 지점에서 의도적 중단")

    return {
        "block": {"id": BID, "name": "오류로그안전성시험",
                  "flood_enabled": True, "traffic_enabled": True},
        "kind": "hls",
        "next_restream_retry_t": float("inf"),
        "perception": SimpleNamespace(step=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("perception 실패(시험)"))),
        "river": SimpleNamespace(at=lambda t: None),
        "river_agent": SimpleNamespace(infer=_stop_after_river),
        "wh": (640, 360), "last_snap_t": 0.0, "roi_rev": config_rev.revision(),
    }


def test_진단_출력_자체가_UnicodeEncodeError를_던져도_스레드는_죽지_않는다(monkeypatch):
    """print()를 가짜로 바꿔 실제 cp949 환경 없이도 같은 실패를 재현한다."""
    runner = _make_runner()
    monkeypatch.setattr(runner, "_next_frame", lambda _c: (0.0, [], None), raising=False)
    c = _make_ctx(runner)

    real_print = builtins.print
    call_count = {"n": 0}

    def _flaky_print(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise UnicodeEncodeError("cp949", "—", 0, 1, "illegal multibyte sequence")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", _flaky_print)

    # perception.step()이 즉시 실패하므로 river_agent까지 못 간다 — 여기서는
    # _block_loop의 예외 처리부(그 안의 print)만 시험하면 된다. 루프가
    # 무한히 돌지 않도록 두 번째 틱에서 stop_ev를 세운다.
    ticks = {"n": 0}
    orig_wait = runner._stop_ev.wait

    def _wait_and_stop(timeout=None):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            runner._stop_ev.set()
        return orig_wait(0)

    monkeypatch.setattr(runner._stop_ev, "wait", _wait_and_stop)

    # ★ 핵심 검증 — 이 호출 자체가 UnicodeEncodeError로 끝나면(스레드가
    # 죽는 것과 같은 뜻) 이 시험이 실패한다. 정상적으로 반환되면(즉
    # _block_loop가 끝까지 스스로 빠져나오면) 방어가 작동한 것이다.
    runner._block_loop(BID, c)

    assert call_count["n"] >= 2, "진단 출력이 아예 시도되지 않았다"
