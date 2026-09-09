# -*- coding: utf-8 -*-
"""회귀 — 위험물 투기 탐지가 ``CrowdLiveAnalyzer.step()``에 실제로 배선돼
있는지 (2026-08-29, 「4대탐지기능 성능개선 로드맵」 5단계).

지켜야 할 것:

* **detector 모드 + 실제 프레임**이 있을 때만 돈다 — mock 모드는 사람
  위치가 화면과 무관해 배경차분 제외 영역이 틀릴 수 있다
  (``abandoned_object.py`` 모듈 docstring 참고)
* 생성 실패(예: cv2 문제)해도 나머지 분석(사람 수·위험도 등)은 계속돼야
  한다 — 새 기능 하나가 기존 파이프라인을 죽이면 안 된다
"""
from __future__ import annotations

import numpy as np
import pytest

from tot_dashboard.crowd.live_analyzer import CrowdLiveAnalyzer, mock_restricted_roi


def _analyzer(source_type: str) -> CrowdLiveAnalyzer:
    cfg = {"intrusion_roi": mock_restricted_roi(), "loiter_sec": 60.0,
           "commercial_zone": True}
    a = CrowdLiveAnalyzer(cfg=cfg, block_id="TEST", node_id="CAM", fps=5.0)
    a.source_type = source_type
    return a


class _FakeDetector:
    def boxes_from_frame(self, frame):
        return np.array([[10.0, 10.0, 50.0, 90.0]]), np.array([0.9])


def test_detector_모드에서_프레임을_주면_위험물_감지기가_생성된다():
    a = _analyzer("detector")
    a.source = _FakeDetector()
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    assert a.abandoned_detector is None
    a.step(1.0, frame_bgr=frame)
    assert a.abandoned_detector not in (None, False), (
        "detector 모드 + 실제 프레임인데 위험물 감지기가 생성되지 않았다")


def test_mock_모드에서는_위험물_감지기가_생성되지_않는다():
    """사람 위치가 화면과 무관한 mock 모드에서 배경차분을 돌리면, 실제
    화면의 사람을 위험물로 오탐할 수 있다 — 아예 돌지 않아야 한다."""
    a = _analyzer("mock")
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    a.step(1.0, frame_bgr=frame)
    assert a.abandoned_detector is None


def test_프레임이_없으면_위험물_감지기가_생성되지_않는다():
    a = _analyzer("detector")
    a.source = _FakeDetector()
    a.step(1.0)   # frame_bgr 없음 — 화면이 부르는 방식(먼저 사람 검출부터 건너뜀)
    assert a.abandoned_detector is None


def test_위험물_감지기_생성이_실패해도_분석은_계속된다(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("cv2 문제(시험)")

    monkeypatch.setattr(
        "tot_dashboard.crowd.abandoned_object.AbandonedObjectDetector", _boom)
    a = _analyzer("detector")
    a.source = _FakeDetector()
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    snap = a.step(1.0, frame_bgr=frame)
    assert snap is not None
    assert snap.to_dict().get("person_count", 0) >= 1
    assert a.abandoned_detector is False, "생성 실패가 기록돼 매 틱 재시도하지 않아야 한다"

    # 다음 틱에서도 재시도 없이(에러 없이) 계속 동작해야 한다.
    snap2 = a.step(2.0, frame_bgr=frame)
    assert snap2 is not None


def test_위험물_이벤트가_있으면_전체_이벤트_목록에_포함된다(monkeypatch):
    from tot_dashboard.crowd.abandoned_object import EVENT_ABANDONED_OBJECT
    from tot_dashboard.crowd.behavior_events import BehaviorEvent

    class _FakeAbandonedDetector:
        def __init__(self, **kw):
            pass

        def update(self, t_sec, frame_bgr, person_boxes=None):
            return [BehaviorEvent(eventType=EVENT_ABANDONED_OBJECT, trackId=1,
                                  confidence=0.7, evidenceText="시험")]

    monkeypatch.setattr(
        "tot_dashboard.crowd.abandoned_object.AbandonedObjectDetector",
        _FakeAbandonedDetector)
    a = _analyzer("detector")
    a.source = _FakeDetector()
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    snap = a.step(1.0, frame_bgr=frame)
    data = snap.to_dict()
    kinds = {e["eventType"] for e in (data.get("events") or [])}
    assert EVENT_ABANDONED_OBJECT in kinds
