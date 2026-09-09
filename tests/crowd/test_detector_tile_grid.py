# -*- coding: utf-8 -*-
"""``DetectorCrowdSource.boxes_from_frame()``의 타일 좌표 복원 정확성
(2026-08-29, 「4대탐지기능 성능개선 로드맵」 1단계 — 원경 인물 검출력 개선).

## 왜 이 시험이 있나

인파 도메인의 타일 분할 추론(2×2)은 데모 1장면으로만 실측됐고
(``live_analyzer.py`` 클래스 docstring 참고), 타일 좌표를 원본 프레임
좌표로 되돌리는 로직(``boxes_from_frame()``) 자체를 직접 검증하는 단위
시험은 없었다. 이번에 3×3·4×4를 관리자가 선택할 수 있게(S-95 전역 설정)
연 만큼, **격자를 세분화해도 좌표 복원이 어긋나지 않는지**를 먼저
고정해 둔다 — 이 저장소가 반복해서 겪은 "해상도·좌표 불일치" 함정과
같은 종류다.

실제 torchvision 모델은 무겁고 이 시험의 관심사(좌표 산술)와 무관하므로
``_infer()``만 가짜로 바꾼다 — 받은 타일(부분 이미지) 안에서 0이 아닌
픽셀의 경계상자를 그대로 돌려주는 단순한 함수다. 원본 프레임에 뚜렷한
표식(흰 사각형)을 두 곳에 두고, 격자를 몇×몇으로 나누든 그 표식이
**원래 있던 자리 그대로** 검출·복원되는지 확인한다.
"""
from __future__ import annotations

import numpy as np
import pytest

from tot_dashboard.crowd.live_analyzer import DetectorCrowdSource


def _make_tiled_source(grid: tuple[int, int]) -> DetectorCrowdSource:
    """무거운 모델 적재(``__init__``) 없이 ``boxes_from_frame()``만 시험한다."""
    src = object.__new__(DetectorCrowdSource)
    src.tile_grid = grid
    src.tile_overlap = 0.2
    src.nms_iou = 0.4
    src.conf = 0.4
    return src


def _fake_infer(self, bgr):
    """받은 타일 안에서 0이 아닌 픽셀의 경계상자 -> (boxes, scores).
    실제 검출기 대신 좌표 산술만 검증하기 위한 결정론적 대역이다."""
    ys, xs = np.nonzero(bgr[:, :, 0])
    if len(xs) == 0:
        return np.zeros((0, 4), np.float32), np.zeros(0, np.float32)
    box = np.array([[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]],
                   dtype=np.float32)
    return box, np.array([0.9], dtype=np.float32)


@pytest.mark.parametrize("grid", [(2, 2), (3, 3), (4, 4)])
def test_표식_두_곳이_원본_좌표_그대로_복원된다(grid, monkeypatch):
    H, W = 300, 400
    frame = np.zeros((H, W, 3), dtype=np.uint8)

    # 서로 다른 타일에 걸리도록 왼쪽 위 근처·오른쪽 아래 근처에 표식을 둔다.
    mark1 = (20, 20, 30, 30)              # (x0, y0, x1, y1) 전역 좌표
    mark2 = (W - 40, H - 40, W - 20, H - 20)
    for (x0, y0, x1, y1) in (mark1, mark2):
        frame[y0:y1, x0:x1, :] = 255

    monkeypatch.setattr(DetectorCrowdSource, "_infer", _fake_infer)
    src = _make_tiled_source(grid)
    boxes, scores = src.boxes_from_frame(frame)

    assert len(boxes) == 2, (
        f"표식 2개가 정확히 복원돼야 한다(실제 {len(boxes)}개) — 좌표 복원이 "
        "어긋나면 같은 표식이 여러 개로 쪼개지거나, 겹치는 타일에서 나온 "
        "서로 다른 좌표가 NMS로 합쳐지지 않고 남는다")

    centers = sorted((float((b[0] + b[2]) / 2), float((b[1] + b[3]) / 2))
                     for b in boxes)
    expected = sorted([
        ((mark1[0] + mark1[2]) / 2, (mark1[1] + mark1[3]) / 2),
        ((mark2[0] + mark2[2]) / 2, (mark2[1] + mark2[3]) / 2),
    ])
    for (cx, cy), (ex, ey) in zip(centers, expected):
        assert abs(cx - ex) <= 1 and abs(cy - ey) <= 1, (
            f"복원된 중심({cx},{cy})이 원본 표식 위치({ex},{ey})와 다르다 — "
            "타일 좌표 -> 원본 좌표 복원(x0/y0 오프셋)이 어긋났다")


def test_격자가_없으면_전체_1회_추론이다(monkeypatch):
    """tile_grid=None 이면 타일 분할 없이 전체 이미지를 한 번에 추론한다
    (기존 동작 — 회귀 방지)."""
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[40:60, 40:60, :] = 255
    monkeypatch.setattr(DetectorCrowdSource, "_infer", _fake_infer)
    src = _make_tiled_source(None)
    boxes, scores = src.boxes_from_frame(frame)
    assert len(boxes) == 1
    assert boxes[0].tolist() == [40.0, 40.0, 60.0, 60.0]


def test_아무것도_없으면_빈_결과다(monkeypatch):
    frame = np.zeros((300, 400, 3), dtype=np.uint8)
    monkeypatch.setattr(DetectorCrowdSource, "_infer", _fake_infer)
    src = _make_tiled_source((3, 3))
    boxes, scores = src.boxes_from_frame(frame)
    assert len(boxes) == 0
