# -*- coding: utf-8 -*-
"""``_block_loop``가 ``kind="restream_unavailable"``인 틱에서 침수·교통위험
판정을 전부 건너뛰는가 — 2026-08-28 신설.

## 왜 이 시험이 있나

``_build_source()``가 재배포 연결 실패를 ``kind="restream_unavailable"``로
표시해도(``test_runner_restream_no_fallback.py``가 그 부분을 고정한다),
``_block_loop``가 이 신호를 실제로 읽어 판정을 막지 않으면 의미가 없다
— SyntheticDetectionSource가 만든 가짜 프레임이 실제 판정처럼 쓰이면
"폴백 없음" 정책이 이름만 남는다.

이 시험은 ``test_flood_disabled_skips_update.py``·
``test_traffic_disabled_skips_judgment.py``와 같은 "정지 신호" 기법을
쓴다 — river_agent.infer는 재배포 상태와 무관하게(하천수위는 카메라
영상과 별개의 계측이므로) 매 틱 불려야 하니, 이 지점을 정지 표지로 삼는다.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tot_dashboard.core import config_rev
from tot_dashboard.service.runner import PipelineRunner

BID = "TEST-RESTREAM-BLOCKLOOP"


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


def _make_ctx(runner, *, kind, flood_calls, vlm_calls, semantic_calls, decider_calls):
    ps = SimpleNamespace(weather=SimpleNamespace(intensity=None),
                         metrics=SimpleNamespace(), vehicles=[], persons=[],
                         incidents=[])

    def _stop_after_river(*a, **k):
        runner._stop_ev.set()
        raise _StopHere("river_agent 지점에서 의도적 중단")

    return {
        "block": {"id": BID, "name": "재배포차단시험",
                  "flood_enabled": True, "traffic_enabled": True},
        "kind": kind,
        # ⚠️ 2026-08-29 — _block_loop가 이제 매 틱 _retry_restream_if_due를
        # 먼저 부른다. 이 시험은 그 재시도 로직이 아니라 "kind별 판정
        # 게이트"만 보는 것이 목적이라, 재시도가 끼어들어 이 손으로 만든
        # 최소 block(source 설정이 없음)을 실제로 재연결 시도해 kind를
        # 조용히 바꿔치기하지 않도록 재시도 시각을 먼 미래로 고정해
        # 매 틱 건너뛰게 한다(재시도 자체는 test_runner_restream_retry.py
        # 가 따로 검증한다).
        "next_restream_retry_t": float("inf"),
        "perception": SimpleNamespace(step=lambda *a, **k: ps),
        "river": SimpleNamespace(at=lambda t: None),
        "river_agent": SimpleNamespace(infer=_stop_after_river),
        "vlm": SimpleNamespace(interpret=lambda *a, **k: vlm_calls.append(1)),
        "semantic": SimpleNamespace(infer=lambda *a, **k: semantic_calls.append(1)),
        # ⚠️ severity=0(임계값 미만)인 실제 객체를 돌려줘야 한다 — 호출부가
        # `decision.severity >= ...`로 알림 여부를 판단하므로, None을
        # 돌려주면(list.append()의 반환값) AttributeError가 나고 그 예외를
        # _block_loop 맨 아래 포괄 except가 삼켜 dt=0.0인 루프가 무한히
        # 돈다(실측 발견, 2026-08-28 — 이 시험 자체가 멈추지 않는 원인이었다).
        "decider": SimpleNamespace(decide=lambda *a, **k: (
            decider_calls.append(1) or SimpleNamespace(severity=0))),
        "wh": (640, 360), "last_snap_t": 0.0, "roi_rev": config_rev.revision(),
    }


def _run_one_tick(runner, c, monkeypatch, flood_calls):
    monkeypatch.setattr(runner, "_next_frame", lambda _c: (0.0, [], None), raising=False)
    monkeypatch.setattr(runner, "_update_flood",
                        lambda *a, **k: flood_calls.append(1), raising=False)
    runner._block_loop(BID, c)


def test_restream_unavailable_틱은_침수_교통_판정을_전부_건너뛴다(monkeypatch):
    runner = _make_runner()
    flood_calls, vlm_calls, sem_calls, dec_calls = [], [], [], []
    c = _make_ctx(runner, kind="restream_unavailable",
                 flood_calls=flood_calls, vlm_calls=vlm_calls,
                 semantic_calls=sem_calls, decider_calls=dec_calls)
    _run_one_tick(runner, c, monkeypatch, flood_calls)
    assert flood_calls == [], "restream_unavailable인데 _update_flood가 불렸다"
    assert vlm_calls == [], "restream_unavailable인데 VLM이 불렸다"
    assert sem_calls == [], "restream_unavailable인데 semantic이 불렸다"
    assert dec_calls == [], "restream_unavailable인데 decider가 불렸다"


def test_정상_kind면_기존처럼_전부_불린다(monkeypatch):
    """회귀 방지 — 게이트를 넣으며 정상(재배포 무관) 카메라의 동작이
    깨지면 안 된다."""
    runner = _make_runner()
    flood_calls, vlm_calls, sem_calls, dec_calls = [], [], [], []
    c = _make_ctx(runner, kind="hls",
                 flood_calls=flood_calls, vlm_calls=vlm_calls,
                 semantic_calls=sem_calls, decider_calls=dec_calls)
    _run_one_tick(runner, c, monkeypatch, flood_calls)
    assert flood_calls == [1]
    assert vlm_calls == [1]
    assert sem_calls == [1]
    assert dec_calls == [1]
