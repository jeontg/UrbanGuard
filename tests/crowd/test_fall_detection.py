# -*- coding: utf-8 -*-
"""쓰러짐(Fall) 이상행동 판정 — 2026-08-29, 「4대탐지기능 성능개선 로드맵」
5단계(GPU 없이 가능한 대체안).

새 모델 없이 검출 상자의 종횡비(가로/세로)만으로 판정한다(``behavior_
events.py`` 모듈 docstring의 원리·한계 참고). 지켜야 할 것:

* 트랙마다 **스스로 기준치(서 있을 때 종횡비)를 잡는다** — 카메라 거리·
  각도에 따라 절대 종횡비가 다르므로 화면 전체에 고정 임계값을 걸면
  안 된다
* 종횡비가 급변해도 **일정 시간 지속돼야** 확정한다 — 순간적인 굽힘·
  가림을 걸러낸다
* 같은 트랙에서 **한 번만** 보고한다
* 기준치가 아직 없는 트랙(처음부터 누워 있는 경우)은 판정하지 않는다 —
  비교할 "서 있을 때" 값 자체가 없다
"""
from __future__ import annotations

from tot_dashboard.crowd.behavior_events import (
    EVENT_FALL,
    FALL_HOLD_SEC,
    BehaviorEventDetector,
)

STANDING = (0.0, 0.0, 20.0, 60.0)   # w=20 h=60 -> ratio 0.33 (서 있음)
FALLEN = (0.0, 0.0, 60.0, 20.0)     # w=60 h=20 -> ratio 3.0 (쓰러짐)


def test_기준치가_잡힌_뒤_종횡비가_급변하고_지속되면_쓰러짐으로_판정한다():
    det = BehaviorEventDetector()
    events = []
    # 기준치(baseline)를 잡을 시간 — 서 있는 채로 몇 틱.
    for t in (0.0, 1.0, 2.0):
        events += det.update(t, [STANDING], [1])
    assert not [e for e in events if e.eventType == EVENT_FALL]

    # 쓰러짐 발생.
    fall_t0 = 3.0
    events += det.update(fall_t0, [FALLEN], [1])
    assert not [e for e in events if e.eventType == EVENT_FALL], (
        "지속시간을 채우기 전이라 아직 확정되면 안 된다")

    # FALL_HOLD_SEC 미만까지는 확정 안 됨.
    events += det.update(fall_t0 + FALL_HOLD_SEC - 0.5, [FALLEN], [1])
    assert not [e for e in events if e.eventType == EVENT_FALL]

    # FALL_HOLD_SEC 이상 지속되면 확정.
    events += det.update(fall_t0 + FALL_HOLD_SEC, [FALLEN], [1])
    fall = [e for e in events if e.eventType == EVENT_FALL]
    assert fall, "지속시간을 채웠는데도 쓰러짐이 보고되지 않았다"
    assert fall[0].trackId == 1
    assert fall[0].bbox == FALLEN
    assert 0.0 < fall[0].confidence <= 1.0


def test_같은_트랙에서_한_번만_보고한다():
    det = BehaviorEventDetector()
    for t in (0.0, 1.0, 2.0):
        det.update(t, [STANDING], [1])
    all_events = []
    for t in (3.0, 3.0 + FALL_HOLD_SEC, 3.0 + FALL_HOLD_SEC + 1.0,
             3.0 + FALL_HOLD_SEC + 2.0):
        all_events += det.update(t, [FALLEN], [1])
    fall_events = [e for e in all_events if e.eventType == EVENT_FALL]
    assert len(fall_events) == 1, "같은 트랙에서 쓰러짐이 중복 보고됐다"


def test_순간적으로_굽혔다_다시_서면_쓰러짐이_아니다():
    """지속시간 요건이 없으면 신발끈을 묶으려 잠깐 숙인 것도 오탐한다 —
    이 시험이 그 방지 장치를 고정한다."""
    det = BehaviorEventDetector()
    events = []
    for t in (0.0, 1.0, 2.0):
        events += det.update(t, [STANDING], [1])
    # 잠깐 종횡비가 커졌다가(굽힘) 지속시간을 채우기 전에 다시 선다.
    events += det.update(3.0, [FALLEN], [1])
    events += det.update(3.5, [STANDING], [1])
    events += det.update(3.5 + FALL_HOLD_SEC, [STANDING], [1])
    assert not [e for e in events if e.eventType == EVENT_FALL]


def test_처음부터_누워있는_트랙은_기준치가_없어_판정하지_않는다():
    """비교할 "서 있을 때" 값 자체가 없는 트랙(예: 검출기가 사람을 뒤늦게
    잡았거나 원래 앉아 있던 자세)은 오탐을 피하기 위해 판정을 보류한다.
    이것이 이 방식의 알려진 한계다(모듈 docstring 참고)."""
    det = BehaviorEventDetector()
    events = []
    for t in (0.0, 1.0, 2.0, 3.0, 3.0 + FALL_HOLD_SEC + 1.0):
        events += det.update(t, [FALLEN], [1])
    assert not [e for e in events if e.eventType == EVENT_FALL]


def test_야간이면_신뢰도가_올라간다():
    from tot_dashboard.crowd.field_sensors import EnvironmentState

    det = BehaviorEventDetector()
    for t in (0.0, 1.0, 2.0):
        det.update(t, [STANDING], [1])
    det.update(3.0, [FALLEN], [1])   # 종횡비 급변 시작(fall_since 설정)
    day_events = det.update(3.0 + FALL_HOLD_SEC, [FALLEN], [1],
                            env=EnvironmentState(t_sec=0.0, is_night=False))

    det2 = BehaviorEventDetector()
    for t in (0.0, 1.0, 2.0):
        det2.update(t, [STANDING], [1])
    det2.update(3.0, [FALLEN], [1])
    night_events = det2.update(3.0 + FALL_HOLD_SEC, [FALLEN], [1],
                               env=EnvironmentState(t_sec=0.0, is_night=True))

    day_fall = [e for e in day_events if e.eventType == EVENT_FALL][0]
    night_fall = [e for e in night_events if e.eventType == EVENT_FALL][0]
    assert night_fall.confidence >= day_fall.confidence
    assert "야간" in night_fall.evidenceText


def test_summary에_쓰러짐_보고_수가_집계된다():
    det = BehaviorEventDetector()
    for t in (0.0, 1.0, 2.0):
        det.update(t, [STANDING], [1])
    det.update(3.0, [FALLEN], [1])
    det.update(3.0 + FALL_HOLD_SEC, [FALLEN], [1])
    assert det.summary()["fall_reported"] == 1
