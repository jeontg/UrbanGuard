"""인파 CCTV 선택 탐지 (crowd/cctv_analysis.py).

실제 스트림 없이 검증하기 위해 cv2.VideoCapture 를 가짜로 갈아끼운다.
검증 대상은 스트림 처리가 아니라 **집계 규칙**이다.
"""
from __future__ import annotations

import sys
import types

import pytest

from tot_dashboard.crowd import cctv_analysis as ca


class _Cam:
    id = "CAM-1"
    name = "테스트지점"
    source_type = "hls"
    source_url = "http://example.invalid/stream.m3u8"


class _Snap:
    """CrowdSnapshot.to_dict() 와 같은 모양만 흉내 낸다."""

    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class _Analyzer:
    """미리 정해 둔 스냅샷을 순서대로 돌려주는 가짜 분석기."""

    def __init__(self, snaps, recent=None):
        self._snaps = list(snaps)
        self._i = 0
        # 상시 루프가 이미 쌓아 둔 이벤트 (공유 상태)
        self._recent_events = list(recent or [])

    def step(self, t_sec, frame):
        d = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return _Snap(d)


class _Cap:
    """duration 이 끝날 때까지 같은 프레임을 계속 내주는 가짜 캡처."""

    def __init__(self, url):
        self.opened = True

    def isOpened(self):
        return self.opened

    def read(self):
        return True, object()

    def release(self):
        self.opened = False


@pytest.fixture
def fake_cv2(monkeypatch):
    mod = types.ModuleType("cv2")
    mod.VideoCapture = _Cap
    mod.imencode = lambda ext, img: (False, None)   # 스냅샷은 이 테스트의 관심사가 아니다
    monkeypatch.setitem(sys.modules, "cv2", mod)
    return mod


def _snap(*, people, severity, risk, events=()):
    return {"person_count": people, "density_index": 0.1, "severity": severity,
            "risk_name": risk, "risk_score": severity / 4, "events": list(events),
            "source": "detector"}


def test_위험등급은_가장_위험했던_순간을_남긴다(fake_cv2):
    """마지막 값을 쓰면 위험이 지나간 뒤 「정상」으로 덮여 관제요원이 놓친다."""
    analyzer = _Analyzer([
        _snap(people=3, severity=0, risk="정상"),
        _snap(people=20, severity=3, risk="군중밀집"),
        _snap(people=4, severity=0, risk="정상"),
    ])
    res = ca.analyze(_Cam(), analyzer, duration_sec=1.0, sample_interval_sec=0.0)

    assert res.severity == 3
    assert res.risk_level == "군중밀집"
    assert res.people_max == 20


def test_관측_이전에_쌓인_이벤트는_결과에_넣지_않는다(fake_cv2):
    """분석기를 상시 루프와 공유하므로, 시작 시점의 이벤트가 섞이면
    30초 관측에 「체류 190초」 같은 값이 나온다."""
    stale = {"eventType": "Loitering", "trackId": 21, "dwellTimeSec": 190.0,
             "confidence": 1.0, "evidenceText": "이전 루프에서 쌓인 것"}
    fresh = {"eventType": "Intrusion", "trackId": 35, "dwellTimeSec": None,
             "confidence": 0.8, "evidenceText": "이번 관측에서 발생"}

    analyzer = _Analyzer(
        # 분석기는 누적 목록을 그대로 돌려준다 (실제 CrowdSnapshot 과 같은 동작)
        [_snap(people=5, severity=1, risk="관심", events=[stale, fresh])],
        recent=[stale],
    )
    res = ca.analyze(_Cam(), analyzer, duration_sec=1.0, sample_interval_sec=0.0)

    kinds = [e["eventType"] for e in res.events]
    assert kinds == ["Intrusion"], "관측 전부터 있던 배회가 결과에 섞였다"


def test_같은_이벤트가_매_프레임_반복돼도_한_번만_담는다(fake_cv2):
    ev = {"eventType": "Intrusion", "trackId": 7, "confidence": 0.9,
          "evidenceText": "통제구역 진입"}
    analyzer = _Analyzer([_snap(people=2, severity=1, risk="관심", events=[ev])] * 5)
    res = ca.analyze(_Cam(), analyzer, duration_sec=1.0, sample_interval_sec=0.0)

    assert len(res.events) == 1


def test_소스가_mock이면_결과에_그대로_표시된다(fake_cv2):
    """mock 이면 분석기가 카메라 프레임을 무시하고 모의 박스를 쓴다.
    화면이 실제 분석 결과로 오해하지 않도록 소스를 함께 내보낸다."""
    d = _snap(people=9, severity=2, risk="주의")
    d["source"] = "mock"
    res = ca.analyze(_Cam(), _Analyzer([d]), duration_sec=1.0, sample_interval_sec=0.0)

    assert res.source == "mock"
    assert res.to_dict()["source"] == "mock"


def test_영상_소스가_없으면_안내만_남기고_끝낸다():
    class _NoSrc:
        id, name, source_type, source_url = "X", "이름", "", ""

    res = ca.analyze(_NoSrc(), _Analyzer([]))
    assert res.frames_analyzed == 0
    assert "영상 소스가 없습니다" in res.note
