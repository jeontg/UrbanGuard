"""물 마스크 경계 상자 — 증거 팝업이 「이벤트가 발생한 부분」에 쓴다 (S-88, 2026-08-20).

## 왜 있나

침수는 개별 탐지가 아니라 영역 세그멘테이션이라, 사람이 쓰는 뜻의 「탐지
상자」가 원래 없다. **지어내지 않는다** — 실제 판정 결과인 마스크 자체의
경계를 상자로 쓴다.
"""
from __future__ import annotations

import numpy as np

from tot_dashboard.flood.water_segmentation import mask_bbox


def test_물이_없으면_None이다():
    mask = np.zeros((100, 100), dtype=np.uint8)
    assert mask_bbox(mask) is None


def test_물_영역의_경계를_정확히_돌려준다():
    mask = np.zeros((100, 200), dtype=np.uint8)
    mask[10:20, 30:50] = 255   # y: 10~19, x: 30~49
    box = mask_bbox(mask)
    assert box == (30, 10, 49, 19)


def test_한_픽셀만_있어도_상자가_된다():
    mask = np.zeros((10, 10), dtype=np.uint8)
    mask[5, 7] = 255
    assert mask_bbox(mask) == (7, 5, 7, 5)


def test_여러_덩어리면_전체를_아우르는_상자다():
    """★ 지어내지 않는다는 원칙 안에서, 여러 물웅덩이가 있으면 그 전체
    범위를 경계로 삼는다 — 개별 폴리곤을 만들어내지 않는다."""
    mask = np.zeros((100, 100), dtype=np.uint8)
    mask[5, 5] = 255
    mask[80, 90] = 255
    assert mask_bbox(mask) == (5, 5, 90, 80)
