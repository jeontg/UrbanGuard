"""``PipelineRunner._block_loop`` 이 ROI 변경을 재기동 없이 반영하는가
(2026-08-22 전수점검).

## 왜 이 시험이 있나

웹 ROI 편집기(S-81)가 ROI 를 저장하면 ``config_rev.bump()`` 신호는 이미
나가고 있었는데 **아무도 구독하지 않았다.** 침수 파이프라인은 기동 시
읽은 ROI 를 프로세스가 죽을 때까지 그대로 물고 있어서, 운영자가 ROI 를
다시 그려도 다음 재기동 전까지 판정이 옛 영역 기준으로 돌았다.

지켜야 할 것.

* 리비전이 바뀌면 **다음 틱에** ROI 를 다시 읽어 엔진에 갈아 끼운다
* 리비전이 그대로면 **DB를 다시 조회하지 않는다** (매 프레임 조회 금지)
* ROI 재조회가 실패해도 **그 틱의 탐지 자체는 계속 돈다** — 재조회 실패
  하나가 프레임 처리를 통째로 건너뛰게 만들면 안 된다
"""
from __future__ import annotations

import threading

import pytest

from tot_dashboard.common.roi import RoiConfig
from tot_dashboard.core import config_rev
from tot_dashboard.service.runner import PipelineRunner

BID = "TEST-ROI-RELOAD"


@pytest.fixture(autouse=True)
def _reset_rev():
    config_rev.bump()  # 다른 시험이 남긴 값과 겹치지 않게 한 번 밀어 둔다
    yield


def _make_runner() -> PipelineRunner:
    r = object.__new__(PipelineRunner)  # 무거운 __init__ 없이 메서드만 시험
    r._stop_ev = threading.Event()
    r.dt = 0.0
    return r


class _SpyEngine:
    def __init__(self):
        self.set_calls: list[RoiConfig] = []

    def set_roi(self, roi):
        self.set_calls.append(roi)


def _run_one_tick(runner, c, monkeypatch):
    """``_block_loop`` 을 딱 한 바퀴만 돌린다.

    ROI 재조회는 루프 본문 **맨 앞**에서 일어나므로, 그 다음 단계인
    ``_next_frame`` 에서 정지 신호를 세우고 예외를 던지면 한 바퀴만 돌고
    빠져나온다(예외는 아래 except 가 삼키고, wait(0) 은 곧바로 반환).
    """
    def _stop_and_raise(_c):
        runner._stop_ev.set()
        raise RuntimeError("한 바퀴만 돌기 위한 의도적 중단")

    monkeypatch.setattr(runner, "_next_frame", _stop_and_raise, raising=False)
    runner._block_loop(BID, c)


def test_리비전이_바뀌면_ROI를_다시_읽어_엔진에_갈아_끼운다(monkeypatch, capsys):
    engine = _SpyEngine()
    new_roi = RoiConfig(camera_name="새ROI", frame_width=640, frame_height=480,
                        road_roi=[[[0, 0], [10, 0], [10, 10]]])
    monkeypatch.setattr("tot_dashboard.service.runner.load_roi_for_camera",
                        lambda *a, **k: new_roi)

    runner = _make_runner()
    c = {"flood_engine": engine, "roi": RoiConfig(),
         "roi_rev": config_rev.revision() - 1,  # 일부러 어긋나게 둔다
         "block": {"flood_enabled": True}}  # 2026-08-23: 게이트 통과 조건

    _run_one_tick(runner, c, monkeypatch)

    assert engine.set_calls == [new_roi], "ROI 를 엔진에 갈아 끼우지 않았다"
    assert c["roi"] is new_roi
    assert c["roi_rev"] == config_rev.revision()


def test_리비전이_그대로면_DB를_다시_조회하지_않는다(monkeypatch):
    """매 프레임 DB를 때리면 상시 탐지가 느려진다."""
    calls = []
    monkeypatch.setattr("tot_dashboard.service.runner.load_roi_for_camera",
                        lambda *a, **k: calls.append(1) or RoiConfig())

    engine = _SpyEngine()
    runner = _make_runner()
    c = {"flood_engine": engine, "roi": RoiConfig(),
         "roi_rev": config_rev.revision(),  # 이미 최신
         "block": {"flood_enabled": True}}

    _run_one_tick(runner, c, monkeypatch)

    assert calls == [], "리비전이 그대로인데 DB를 다시 조회했다"
    assert engine.set_calls == []


def test_ROI_재조회가_실패해도_탐지_루프는_계속_돈다(monkeypatch, capsys):
    """★ 재조회 실패 하나가 그 틱의 프레임 처리를 통째로 건너뛰면 안 된다."""
    def _boom(*a, **k):
        raise RuntimeError("DB 장애 흉내")

    monkeypatch.setattr("tot_dashboard.service.runner.load_roi_for_camera", _boom)

    reached = []
    runner = _make_runner()

    def _stop_and_record(_c):
        reached.append(True)          # ← 여기까지 왔다는 것이 이 시험의 핵심
        runner._stop_ev.set()
        raise RuntimeError("한 바퀴만 돌기 위한 의도적 중단")

    monkeypatch.setattr(runner, "_next_frame", _stop_and_record, raising=False)

    c = {"flood_engine": _SpyEngine(), "roi": RoiConfig(),
         "roi_rev": config_rev.revision() - 1,
         "block": {"flood_enabled": True}}
    runner._block_loop(BID, c)

    assert reached == [True], "ROI 재조회 실패가 프레임 처리까지 막았다"
    assert "ROI 재조회 실패" in capsys.readouterr().out
