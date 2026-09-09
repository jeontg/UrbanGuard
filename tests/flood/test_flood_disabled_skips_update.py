# -*- coding: utf-8 -*-
"""``_block_loop`` 이 ``flood_enabled=False`` 인 블록에서 침수 판정을
아예 돌리지 않는가 — 2026-08-23 실사용 중 발견.

## 왜 이 시험이 있나

교통위험만 상시로 켠(침수는 미사용) 카메라가 처리 루프에 새로 올라오게
되면서(``continuous_blocks_any``), **그 블록이 예전에 침수 ROI를 그려
둔 적이 있으면** ``_update_flood()``가 그 보관된 ROI로 물 세그멘테이션을
계속 돌리고 심하면 침수 알림까지 낼 위험이 생겼다 — 운영자가 침수
지정을 분명히 껐는데도.

지켜야 할 것.

* ``flood_enabled=False`` 면 ``_update_flood()``를 **절대 부르지 않는다**
* ``flood_enabled=True`` 면 여전히 매 틱 부른다(회귀 방지)
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tot_dashboard.core import config_rev
from tot_dashboard.service.runner import PipelineRunner

BID = "TEST-FLOOD-GATE"


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
    """flood_enabled 게이트를 통과한 직후(다음 줄)에서 멈추기 위한 신호."""


def _make_ctx(runner, *, flood_enabled: bool):
    ps = SimpleNamespace(weather=SimpleNamespace(intensity=None),
                         metrics=SimpleNamespace(), vehicles=[], persons=[])

    def _stop_after_gate(*a, **k):
        # ⚠️ 여기서 멈추지 않으면 _block_loop 의 while 이 이 지점을 계속
        # 다시 부르며 무한 루프가 된다(예외는 바깥 except가 삼키고 다음
        # 바퀴를 또 돈다) — ROI 재조회 시험(test_runner_roi_reload.py)과
        # 같은 이유로, 정지 신호를 raise **전에** 반드시 세운다.
        runner._stop_ev.set()
        raise _StopHere("게이트 통과 직후 의도적 중단")

    perception = SimpleNamespace(step=lambda *a, **k: ps)
    vlm = SimpleNamespace(interpret=_stop_after_gate)
    river = SimpleNamespace(at=lambda t: None)

    return {
        # traffic_enabled=True 로 고정 — 이 시험은 flood 게이트 전용이다.
        # 2026-08-28에 traffic_enabled 게이트가 추가되며 vlm.interpret가
        # 더는 무조건 불리지 않게 됐다. 여기서 False(기본값)로 두면
        # `_stop_after_gate`(vlm.interpret에 심어 둔 정지 신호)가 아예
        # 불리지 않아 루프가 멈추지 않는다 — traffic 게이트 자체는
        # test_traffic_disabled_skips_judgment.py 가 따로 검증한다.
        "block": {"id": BID, "name": "게이트시험", "flood_enabled": flood_enabled,
                  "traffic_enabled": True},
        "perception": perception, "river": river, "vlm": vlm,
        "wh": (640, 360), "last_snap_t": 0.0, "roi_rev": config_rev.revision(),
    }


def _run_one_tick(runner, c, monkeypatch, flood_spy):
    def _next_frame(_c):
        return (0.0, [], None)

    monkeypatch.setattr(runner, "_next_frame", _next_frame, raising=False)
    monkeypatch.setattr(runner, "_update_flood", flood_spy, raising=False)
    runner._block_loop(BID, c)


def test_flood_enabled_false면_update_flood를_부르지_않는다(monkeypatch, capsys):
    runner = _make_runner()
    c = _make_ctx(runner, flood_enabled=False)
    calls = []
    _run_one_tick(runner, c, monkeypatch, lambda *a, **k: calls.append(1))
    assert calls == [], (
        "flood_enabled=False 인데 _update_flood 가 불렸다 — 침수 지정을 "
        "꺼도 보관된 ROI로 판정이 계속 도는 회귀")


def test_flood_enabled_true면_여전히_매_틱_부른다(monkeypatch, capsys):
    """회귀 방지 — 게이트를 넣으며 기존 카메라(침수 상시)의 동작이 깨지면 안 된다."""
    runner = _make_runner()
    c = _make_ctx(runner, flood_enabled=True)
    calls = []
    _run_one_tick(runner, c, monkeypatch, lambda *a, **k: calls.append(1))
    assert calls == [1], "flood_enabled=True 인데 _update_flood 가 안 불렸다"
