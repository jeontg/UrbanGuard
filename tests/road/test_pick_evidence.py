"""증거로 남길 대표 프레임·상자 고르기 — S-88 증거 팝업 (2026-08-20).

## 왜 있나

노면은 15초 동안 여러 프레임을 본다. 손상이 여러 프레임에 걸쳐 있으면
**상자와 화면이 서로 다른 순간의 것**이 되어 「여기서 났다」가 거짓말이
된다. 그래서 **손상이 가장 많이 잡힌 프레임 하나만** 고르고, 그 프레임의
상자만 쓴다 — 다른 프레임의 손상과 섞지 않는다.
"""
from __future__ import annotations

import numpy as np

from tot_dashboard.road.live_analyzer import RoadDefectAnalyzer


def _frames(n: int):
    return [(i, np.zeros((10, 10, 3), dtype=np.uint8)) for i in range(n)]


def test_손상이_없으면_아무것도_고르지_않는다():
    frame, boxes = RoadDefectAnalyzer._pick_evidence(_frames(3), [])
    assert frame is None
    assert boxes == []


def test_프레임이_없으면_고를_수_없다():
    frame, boxes = RoadDefectAnalyzer._pick_evidence(
        [], [{"frame": 0, "type": "pothole", "confidence": 0.9, "box": [1, 1, 2, 2]}])
    assert frame is None
    assert boxes == []


def test_손상이_가장_많은_프레임을_고른다():
    frames = _frames(3)
    defects = [
        {"frame": 0, "type": "pothole", "confidence": 0.5, "box": [1, 1, 2, 2]},
        {"frame": 1, "type": "crack", "confidence": 0.6, "box": [3, 3, 4, 4]},
        {"frame": 1, "type": "pothole", "confidence": 0.7, "box": [5, 5, 6, 6]},
    ]
    frame, boxes = RoadDefectAnalyzer._pick_evidence(frames, defects)
    assert frame is frames[1][1]        # 프레임 1 이 손상 2건으로 최다
    assert len(boxes) == 2
    assert {b["label"] for b in boxes} == {"crack", "pothole"}


def test_다른_프레임의_손상과_섞이지_않는다():
    """★ 이게 이 기능의 알맹이다 — 선택되지 않은 프레임의 손상은 상자에
    나오면 안 된다."""
    frames = _frames(2)
    defects = [
        {"frame": 0, "type": "pothole", "confidence": 0.9, "box": [1, 1, 2, 2]},
        {"frame": 1, "type": "crack", "confidence": 0.5, "box": [3, 3, 4, 4]},
        {"frame": 1, "type": "crack", "confidence": 0.5, "box": [5, 5, 6, 6]},
    ]
    _, boxes = RoadDefectAnalyzer._pick_evidence(frames, defects)
    assert all(b["label"] == "crack" for b in boxes)
    assert len(boxes) == 2


def test_상자_좌표가_그대로_옮겨진다():
    frames = _frames(1)
    defects = [{"frame": 0, "type": "pothole", "confidence": 0.9,
               "box": [10, 20, 30, 40]}]
    _, boxes = RoadDefectAnalyzer._pick_evidence(frames, defects)
    assert boxes == [{"x1": 10, "y1": 20, "x2": 30, "y2": 40, "label": "pothole"}]
