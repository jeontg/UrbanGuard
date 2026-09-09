"""검출기 소스가 프레임 없이 불릴 때 (crowd/live_analyzer.py).

발견 경위
    오류 관리(S-92)가 ``/api/crowd/live`` 에서 **12회 반복된 500** 을 잡아냈다.
    ``AttributeError: 'DetectorCrowdSource' object has no attribute 'boxes_at'``.
    화면이 주기적으로 부르는 경로라 **조용히 계속 실패하고 있었다.**

지켜야 할 것
    * 프레임이 없으면 **「관측하지 못했다」이지 오류가 아니다** — 빈 결과로
      진행하고 나머지 지표는 정상적으로 내려 준다
    * mock 소스는 원래대로 시간 기반으로 동작한다
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
    """실제 검출기와 같은 인터페이스 — ``boxes_at`` 이 **없다**."""

    def boxes_from_frame(self, frame):
        return np.array([[10.0, 10.0, 50.0, 90.0]]), np.array([0.9])


def test_검출기_소스가_프레임_없이_불려도_터지지_않는다():
    """이게 깨지면 /api/crowd/live 가 500 을 낸다."""
    a = _analyzer("detector")
    a.source = _FakeDetector()
    snap = a.step(1.0)          # 프레임 없음 — 화면이 부르는 방식
    assert snap is not None


def test_프레임이_없으면_사람은_0명이다():
    """못 본 것을 「0명 관측」으로 기록하는 것이 아니라, 관측 자체가 없었다."""
    a = _analyzer("detector")
    a.source = _FakeDetector()
    snap = a.step(1.0)
    data = snap.to_dict()
    assert data.get("person_count", 0) == 0


def test_프레임을_주면_검출한다():
    a = _analyzer("detector")
    a.source = _FakeDetector()
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    snap = a.step(1.0, frame_bgr=frame)
    assert snap.to_dict().get("person_count", 0) >= 1


def test_mock_소스는_그대로_동작한다():
    """고치면서 기존 경로를 망가뜨리지 않았는지."""
    a = _analyzer("mock")
    snap = a.step(1.0)
    assert snap is not None


def test_여러_번_불러도_계속_동작한다():
    """화면은 이 API 를 주기적으로 부른다. 한 번만 되는 것으로는 부족하다."""
    a = _analyzer("detector")
    a.source = _FakeDetector()
    for t in range(1, 6):
        assert a.step(float(t)) is not None
