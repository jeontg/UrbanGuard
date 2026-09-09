# -*- coding: utf-8 -*-
"""``_block_loop`` 이 ``traffic_enabled=False`` 인 블록에서 교통위험
판정(VLM·semantic·decider)을 아예 돌리지 않는가 — 2026-08-28 실사용 점검
중 발견.

## 왜 이 시험이 있나

``continuous_blocks_any()``(2026-08-23)가 침수·교통위험 어느 한쪽만
상시여도 카메라를 처리 루프에 올리게 되면서, **침수만 상시로 켠 카메라
(교통위험 미지정)에서도 VLM 호출·semantic·decider·SOLAPI 교통위험
알림이 조용히 계속 돌고 있었다** — flood_enabled 게이트(2026-08-23)는
``_update_flood()`` 만 막았을 뿐, 대칭인 traffic 쪽 게이트가 없었다.

지켜야 할 것.

* ``traffic_enabled=False`` 면 VLM·semantic·decider를 **절대 부르지 않는다**
* ``traffic_enabled=True`` 면 여전히 매 틱 부른다(회귀 방지)
* 침수는 이 게이트와 무관하게 그대로 돈다 — perception.step()(차량·보행자
  위치)은 두 경우 모두 계속 불려야 한다(``_update_flood``가 그 결과를 씀)
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tot_dashboard.core import config_rev
from tot_dashboard.models import RiskLevel
from tot_dashboard.service.runner import PipelineRunner, _no_traffic_decision

BID = "TEST-TRAFFIC-GATE"


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
    """정지 지점(river_agent.infer — 두 분기 모두 그 뒤에 도달)에서
    한 바퀴만 돌고 멈추기 위한 신호."""


def _make_ctx(runner, *, traffic_enabled: bool, vlm_spy, semantic_spy, decider_spy):
    ps = SimpleNamespace(weather=SimpleNamespace(intensity=None),
                         metrics=SimpleNamespace(), vehicles=[], persons=[],
                         incidents=[])

    def _stop_after_gate(*a, **k):
        # ⚠️ flood 게이트 시험(test_flood_disabled_skips_update.py)과 같은
        # 이유로, 정지 신호를 raise **전에** 반드시 세운다 — 안 그러면
        # dt=0.0 인 while 루프가 무한히 돈다.
        runner._stop_ev.set()
        raise _StopHere("정지 지점 통과 직후 의도적 중단")

    perception = SimpleNamespace(step=lambda *a, **k: ps)
    vlm = SimpleNamespace(interpret=vlm_spy)
    semantic = SimpleNamespace(infer=semantic_spy)
    decider = SimpleNamespace(decide=decider_spy)
    river = SimpleNamespace(at=lambda t: None)
    river_agent = SimpleNamespace(infer=_stop_after_gate)

    return {
        # flood_enabled=False 로 둔다 — 이 시험은 traffic 게이트 전용이라
        # _update_flood() 호출 여부(별도 시험 소관)와 섞이면 안 된다.
        "block": {"id": BID, "name": "교통게이트시험", "flood_enabled": False,
                  "traffic_enabled": traffic_enabled},
        "perception": perception, "river": river, "river_agent": river_agent,
        "vlm": vlm, "semantic": semantic, "decider": decider,
        "wh": (640, 360), "last_snap_t": 0.0, "roi_rev": config_rev.revision(),
    }


def _run_one_tick(runner, c, monkeypatch):
    def _next_frame(_c):
        return (0.0, [], None)

    monkeypatch.setattr(runner, "_next_frame", _next_frame, raising=False)
    monkeypatch.setattr(runner, "_update_flood", lambda *a, **k: None, raising=False)
    # ⚠️ _StopHere 는 _block_loop 안의 `except Exception`이 삼킨다 —
    # 여기까지 새어 나오지 않는다(flood 게이트 시험과 같은 구조).
    # 루프를 멈추는 것은 예외가 아니라 그 직전에 세운 _stop_ev 다.
    runner._block_loop(BID, c)


def test_traffic_enabled_false면_VLM_semantic_decider를_부르지_않는다(monkeypatch):
    runner = _make_runner()
    vlm_calls, sem_calls, dec_calls = [], [], []
    c = _make_ctx(runner, traffic_enabled=False,
                 vlm_spy=lambda *a, **k: vlm_calls.append(1),
                 semantic_spy=lambda *a, **k: sem_calls.append(1),
                 decider_spy=lambda *a, **k: dec_calls.append(1))
    _run_one_tick(runner, c, monkeypatch)
    assert vlm_calls == [], "traffic_enabled=False 인데 VLM이 불렸다"
    assert sem_calls == [], "traffic_enabled=False 인데 semantic이 불렸다"
    assert dec_calls == [], "traffic_enabled=False 인데 decider가 불렸다"


def test_traffic_enabled_true면_여전히_매_틱_VLM을_부른다(monkeypatch):
    """회귀 방지 — 게이트를 넣으며 기존 카메라(교통위험 상시)의 동작이
    깨지면 안 된다. VLM이 정지 지점을 겸한다(flood 시험과 같은 방식)."""
    runner = _make_runner()
    vlm_calls = []

    def _vlm_stop(*a, **k):
        vlm_calls.append(1)
        runner._stop_ev.set()
        raise _StopHere("VLM 호출 확인 후 의도적 중단")

    c = _make_ctx(runner, traffic_enabled=True, vlm_spy=_vlm_stop,
                 semantic_spy=lambda *a, **k: None, decider_spy=lambda *a, **k: None)
    _run_one_tick(runner, c, monkeypatch)
    assert vlm_calls == [1], "traffic_enabled=True 인데 VLM이 안 불렸다"


def test_no_traffic_decision은_지어낸_경보가_아니다():
    """중립값 — 등급은 가장 낮은 「관심」, 심각도 0, 경보 없음, risk_code
    는 TWR_* 네임스페이스 밖(빈 문자열)이어야 한다."""
    d = _no_traffic_decision(12.3)
    assert d.t_sec == 12.3
    assert d.risk_code == ""
    assert d.level == RiskLevel.interest
    assert d.severity == 0
    assert d.score == 0.0
    assert d.alert is False
