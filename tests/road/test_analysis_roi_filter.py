"""``analysis_roi`` 가 실제 노면 손상 판정에 쓰이는가 (2026-08-22 전수점검).

## 왜 이 시험이 있나

S-81 에서 저장한 노면 ``analysis_roi`` 가 **저장만 되고 판정에는 전혀 쓰이지
않았다.** 화면은 "설정하지 않으면 판정이 부정확합니다"라고 안내하는데 실제로는
설정해도 달라지는 것이 없었다.

⚠️ **한계** — 현재 노면 모델은 부산 CCTV 실측 탐지 0건(RDD2022 도메인 갭)이라
**실제 효과는 모델 교체 후에야 확인할 수 있다.** 여기서는 탐지기를 몽키패치해
필터 로직 자체만 못박아 둔다.
"""
from __future__ import annotations

import numpy as np
import pytest

from tot_dashboard.road.live_analyzer import RoadDefectAnalyzer

FRAME_WH = (400, 200)
LEFT_HALF = [[[0, 0], [200, 0], [200, 200], [0, 200]]]


class _Defect:
    def __init__(self, box, cls_name="pothole", confidence=0.9):
        self.box = box
        self.cls_name = cls_name
        self.confidence = confidence


class _Result:
    def __init__(self, defects):
        self.defects = defects


@pytest.fixture()
def analyzer(monkeypatch):
    a = RoadDefectAnalyzer()
    monkeypatch.setattr(a, "_ensure_model", lambda: object())
    # 좌(ROI 안) 1건 + 우(ROI 밖) 2건을 항상 돌려주는 가짜 탐지기
    monkeypatch.setattr(
        "tot_dashboard.road.defect_detection.detect_defects",
        lambda model, frame, conf=0.0: _Result([
            _Defect([20, 100, 60, 150]),     # 중심 (40,125) → ROI 안
            _Defect([300, 100, 340, 150]),   # 중심 (320,125) → ROI 밖
            _Defect([350, 100, 390, 150]),   # 중심 (370,125) → ROI 밖
        ]))
    return a


def _frames():
    return [(0, np.zeros((FRAME_WH[1], FRAME_WH[0], 3), dtype=np.uint8))]


def test_ROI_밖_손상은_결과에서_빠진다(analyzer):
    out = analyzer._analyze_frames(
        _frames(), conf=0.25,
        roi={"analysis_roi": LEFT_HALF,
             "roi_frame_width": FRAME_WH[0], "roi_frame_height": FRAME_WH[1]})
    assert len(out) == 1, f"ROI 밖 손상까지 셌다: {out}"
    assert out[0]["box"] == [20, 100, 60, 150]


def test_ROI_미설정이면_전부_그대로(analyzer):
    """★ analysis_roi 는 required=False 라 미설정 카메라가 많다 —
    이 폴백이 이번 변경의 가장 중요한 회귀 방지 지점이다."""
    assert len(analyzer._analyze_frames(_frames(), conf=0.25, roi=None)) == 3
    assert len(analyzer._analyze_frames(_frames(), conf=0.25, roi={})) == 3
    assert len(analyzer._analyze_frames(
        _frames(), conf=0.25, roi={"analysis_roi": []})) == 3


def test_해상도가_다르면_ROI_좌표를_보정한다(analyzer):
    out = analyzer._analyze_frames(
        _frames(), conf=0.25,
        roi={"analysis_roi": [[[0, 0], [400, 0], [400, 400], [0, 400]]],
             "roi_frame_width": 800, "roi_frame_height": 400})
    assert len(out) == 1


def test_프레임_자체는_마스킹하지_않는다(analyzer, monkeypatch):
    """★ 픽셀을 검게 칠하면 그 인위적 경계를 모델이 손상으로 오탐할 수 있다 —
    탐지는 항상 원본 전체 프레임에 돌린다."""
    seen = []
    monkeypatch.setattr(
        "tot_dashboard.road.defect_detection.detect_defects",
        lambda model, frame, conf=0.0: seen.append(frame.copy()) or _Result([]))

    frames = _frames()
    original = frames[0][1].copy()
    analyzer._analyze_frames(
        frames, conf=0.25,
        roi={"analysis_roi": LEFT_HALF,
             "roi_frame_width": FRAME_WH[0], "roi_frame_height": FRAME_WH[1]})

    assert len(seen) == 1
    assert np.array_equal(seen[0], original), "탐지 전에 프레임을 건드렸다"
