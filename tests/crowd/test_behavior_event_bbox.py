"""배회·침입 이벤트의 상자(bbox) — 증거 팝업이 「이벤트가 발생한 부분」에
쓴다 (S-88, 2026-08-20).

**지어내지 않는다** — 이 이벤트를 만든 바로 그 트랙의 실제 추적 상자다.
`position`(발밑 점 하나)보다 구체적이라 상자로도 남긴다.
"""
from __future__ import annotations

from tot_dashboard.crowd.behavior_events import (BehaviorEventDetector,
                                                 EVENT_INTRUSION,
                                                 EVENT_LOITERING)


def test_배회_이벤트에_상자가_담긴다():
    det = BehaviorEventDetector(loiter_sec=1.0, loiter_radius_px=50.0)
    box = (10.0, 20.0, 30.0, 60.0)
    events = []
    for t in (0.0, 0.5, 1.0, 1.5):
        events += det.update(t, [box], [1])
    loiter = [e for e in events if e.eventType == EVENT_LOITERING]
    assert loiter, "배회 이벤트가 발생하지 않았습니다"
    assert loiter[0].bbox == box
    assert loiter[0].to_dict()["bbox"] == [10.0, 20.0, 30.0, 60.0]


def test_침입_이벤트에_상자가_담긴다():
    # 사각형 (0,0)-(100,100) 을 통제구역으로 둔다.
    roi = [[[0, 0], [100, 0], [100, 100], [0, 100]]]
    det = BehaviorEventDetector(intrusion_roi=roi)
    outside_box = (200.0, 200.0, 220.0, 240.0)
    inside_box = (10.0, 10.0, 30.0, 50.0)
    # 최초 관측은 밖 — 아직 침입이 아니다.
    events = det.update(0.0, [outside_box], [7])
    assert not [e for e in events if e.eventType == EVENT_INTRUSION]
    # 안으로 들어온 순간 침입 이벤트가 뜬다.
    events = det.update(1.0, [inside_box], [7])
    intrusion = [e for e in events if e.eventType == EVENT_INTRUSION]
    assert intrusion, "침입 이벤트가 발생하지 않았습니다"
    assert intrusion[0].bbox == inside_box


def test_상자가_없으면_None이다():
    """일반 BehaviorEvent(배회·침입 아닌 경우를 만들 수단이 없으므로,
    필드 기본값 자체가 None 인지만 확인한다 — 지어내지 않는다는 원칙."""
    from tot_dashboard.crowd.behavior_events import BehaviorEvent
    ev = BehaviorEvent(eventType="X", trackId=1, confidence=0.5,
                       evidenceText="")
    assert ev.bbox is None
    assert ev.to_dict()["bbox"] is None
