# -*- coding: utf-8 -*-
"""위험물 투기(Abandoned Object) 탐지 — 2026-08-29, 「4대탐지기능 성능개선
로드맵」 5단계(GPU 없이 가능한 대체안).

배경차분(MOG2)만으로 판정한다(``abandoned_object.py`` 모듈 docstring의
원리·한계 참고). 지켜야 할 것:

* 배경에 **새로 나타나 오래 머무는** 전경만 확정한다 — 지나가는 사람처럼
  계속 움직이는 전경은 걸러진다
* 사람 상자와 겹치는 자리는 물체 후보에서 뺀다
* 같은 물체에서 한 번만 보고한다
"""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from tot_dashboard.crowd.abandoned_object import (  # noqa: E402
    EVENT_ABANDONED_OBJECT,
    PERSIST_SEC,
    AbandonedObjectDetector,
)


def _blank_frame(h=240, w=320) -> np.ndarray:
    rng = np.random.default_rng(0)
    # 완전한 단색은 MOG2가 배경으로 너무 빨리 흡수하지 않도록 약한 잡음을 준다.
    return (rng.integers(60, 70, (h, w, 3))).astype(np.uint8)


def _with_object(frame: np.ndarray, box) -> np.ndarray:
    x0, y0, x1, y1 = box
    out = frame.copy()
    out[y0:y1, x0:x1, :] = 220
    return out


def test_배경만_있으면_확정되지_않는다():
    det = AbandonedObjectDetector(persist_sec=1.0)
    frame = _blank_frame()
    events = []
    for t in range(20):
        events += det.update(float(t) * 0.1, frame)
    assert not [e for e in events if e.eventType == EVENT_ABANDONED_OBJECT]


def test_새로_나타나_지속되면_확정된다():
    det = AbandonedObjectDetector(persist_sec=1.0)
    bg = _blank_frame()
    box = (100, 100, 140, 140)
    events = []
    # 배경을 먼저 몇 프레임 학습시킨다.
    for t in range(35):
        events += det.update(float(t) * 0.2, bg)
    assert not [e for e in events if e.eventType == EVENT_ABANDONED_OBJECT]

    # 이제 물체가 나타나 계속 같은 자리에 머문다.
    frame_with_obj = _with_object(bg, box)
    t0 = 2.0
    for i in range(15):
        events += det.update(t0 + i * 0.2, frame_with_obj)

    obj_events = [e for e in events if e.eventType == EVENT_ABANDONED_OBJECT]
    assert obj_events, "지속된 물체가 확정되지 않았다"
    assert 0.0 < obj_events[0].confidence <= 1.0


def test_같은_물체는_한_번만_보고한다():
    det = AbandonedObjectDetector(persist_sec=0.5)
    bg = _blank_frame()
    box = (60, 60, 100, 100)
    for t in range(35):
        det.update(float(t) * 0.1, bg)

    frame_with_obj = _with_object(bg, box)
    all_events = []
    for i in range(30):
        all_events += det.update(1.0 + i * 0.2, frame_with_obj)

    reported = [e for e in all_events if e.eventType == EVENT_ABANDONED_OBJECT]
    assert len(reported) == 1, "같은 물체가 중복 보고됐다"


def test_사람_영역은_물체_후보에서_제외된다():
    """사람이 서 있는 자리에 생긴 전경은 물체로 오인하면 안 된다."""
    det = AbandonedObjectDetector(persist_sec=0.5)
    bg = _blank_frame()
    person_box = (100, 100, 140, 180)   # 사람 하나가 서 있는 자리
    for t in range(35):
        det.update(float(t) * 0.1, bg, person_boxes=[person_box])

    # 사람이 서 있는 바로 그 자리에 "전경"이 생겨도(사람 자체가 전경이므로)
    frame_with_person = _with_object(bg, person_box)
    events = []
    for i in range(15):
        events += det.update(1.0 + i * 0.2, frame_with_person,
                             person_boxes=[person_box])
    assert not [e for e in events if e.eventType == EVENT_ABANDONED_OBJECT], (
        "사람이 서 있는 자리를 위험물로 오탐했다")


def test_summary가_후보_수를_보여준다():
    det = AbandonedObjectDetector(persist_sec=100.0)  # 확정되지 않게 크게
    bg = _blank_frame()
    box = (60, 60, 100, 100)
    for t in range(5):
        det.update(float(t) * 0.1, bg)
    frame_with_obj = _with_object(bg, box)
    det.update(1.0, frame_with_obj)
    s = det.summary()
    assert s["active_candidates"] >= 1
    assert s["reported"] == 0
