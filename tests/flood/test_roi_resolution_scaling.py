# -*- coding: utf-8 -*-
"""ROI 저장 해상도 ≠ 실제 처리 해상도일 때의 판정 보정 — 2026-08-23 신설.

## 왜 이 시험이 있나

실사용 중 발견(SEOUL-1042 등): ROI는 **저장 당시 정지영상 해상도** 기준
픽셀 좌표인데, 침수 판정 파이프라인(``RoiMaskCache.ensure()``)은 그 좌표를
**실제 처리 프레임(물 세그멘테이션 마스크) 해상도**에 그대로 씌우고
있었다. 두 해상도가 다르면(스트림 화질 변경, 캘리브레이션 때와 다른
소스로 다시 그림 등) 도로 ROI가 화면 밖 엉뚱한 자리를 가리켜 물 면적
비율이 조용히 0에 가깝게 나온다 — "도로 ROI를 설정했는데도 침수가 전혀
안 잡힌다"는 형태로 나타나는, 화면 표시 버그(camera_roi.html)와 같은
근본 원인의 판정 버그다.

지켜야 할 것.

* 해상도가 다르면 **비례 보정**해서 마스크를 만든다(``scale_polygons``와
  같은 방식)
* 해상도가 같거나(또는 저장 해상도를 모르면) **원본 그대로** 쓴다 —
  이 코드베이스의 일관된 안전 폴백 원칙
* road_mask 뿐 아니라 low_point_roi(저지대)·lane_threshold_line(차선
  기준선)도 같은 보정을 받아야 한다 — 마스크만 고치고 이 둘을 빼먹으면
  "저지대 침수는 여전히 안 잡히는" 절반짜리 수정이 된다
"""
from __future__ import annotations

import numpy as np

from tot_dashboard.common.roi import RoiConfig
from tot_dashboard.flood.flood_metrics_engine import FloodMetricsEngine
from tot_dashboard.flood.metrics_core import RoiMaskCache
from tot_dashboard.flood.standalone_pipeline import StandaloneMetricsEngine
from tot_dashboard.flood.water_segmentation import WaterResult


def _square(x, y, size):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def _water(mask: np.ndarray) -> WaterResult:
    return WaterResult(mask=mask, num_instances=1 if mask.any() else 0,
                       water_pixels=int(np.count_nonzero(mask)),
                       max_confidence=0.9 if mask.any() else 0.0)


# --- RoiMaskCache 단위 --------------------------------------------------------

def test_해상도가_같으면_원본_그대로_쓴다():
    roi = RoiConfig(frame_width=100, frame_height=100,
                    road_roi=[_square(10, 10, 50)])
    cache = RoiMaskCache(roi)
    cache.ensure(100, 100)
    assert cache.scaled.road_roi == roi.road_roi
    # 정확한 픽셀 수는 cv2.fillPoly 의 경계 처리 방식에 달려 있어(테두리
    # 포함 여부) 50x50 이 아니라 51x51 로 나올 수 있다 — 여기서 확인할
    # 것은 "보정을 안 거쳤다"이지 정확한 래스터화 픽셀 수가 아니다.
    assert 2400 <= cache.road_area <= 2700


def test_해상도가_2배_다르면_면적도_비례해서_커진다():
    """도로 ROI를 200x200 정지영상에 그렸는데(50x50 정사각형, 면적 2500),
    실제 처리 프레임이 100x100(절반)이면 마스크 면적도 절반 비율로
    줄어야 한다(625 = (25x25)) — 좌표를 그대로 썼다면 마스크가 화면
    절반을 넘어가 잘리거나, 반대 방향 스케일이면 아예 화면 밖으로
    나가 면적이 0이 된다."""
    roi = RoiConfig(frame_width=200, frame_height=200,
                    road_roi=[_square(20, 20, 50)])
    cache = RoiMaskCache(roi)
    cache.ensure(100, 100)  # 처리 프레임은 절반 크기
    # 기대값은 (50*0.5)^2=625 이지만 cv2.fillPoly 경계 처리로 몇 픽셀
    # 오차가 있을 수 있다 — 근사 범위로 "절반 비율로 줄었다"만 확인한다.
    assert 550 <= cache.road_area <= 750
    # 스케일된 좌표가 새 캔버스 안에 들어와 있어야 한다
    poly = cache.scaled.road_roi[0]
    assert all(0 <= x <= 100 and 0 <= y <= 100 for x, y in poly)


def test_저장_해상도가_훨씬_크면_원본_좌표는_화면_밖으로_나간다():
    """실사용에서 실제로 겪은 형태 — 1920x1080에 그린 ROI를 640x360
    프레임에 그대로 씌우면 마스크 면적이 0에 수렴한다(보정이 없다면).
    보정 후에는 정상적인 면적이 나와야 한다."""
    roi = RoiConfig(frame_width=1920, frame_height=1080,
                    road_roi=[[[166, 1054], [1811, 1036], [1000, 523]]])
    cache = RoiMaskCache(roi)
    cache.ensure(360, 640)  # h=360, w=640
    assert cache.road_area > 0
    poly = cache.scaled.road_roi[0]
    assert all(0 <= x <= 640 and 0 <= y <= 360 for x, y in poly)


def test_저장_해상도를_모르면_보정하지_않고_원본을_그대로_쓴다():
    """scale_polygons()의 안전한 폴백과 같은 원칙 — 기준을 모르면 잘못
    늘이는 것보다 원본 그대로 쓰는 편이 낫다(레거시 ROI 파일 등)."""
    roi = RoiConfig(frame_width=None, frame_height=None,
                    road_roi=[_square(10, 10, 50)])
    cache = RoiMaskCache(roi)
    cache.ensure(100, 100)
    assert cache.scaled.road_roi == roi.road_roi


def test_저지대와_차선_기준선도_함께_보정된다():
    roi = RoiConfig(frame_width=200, frame_height=200,
                    road_roi=[_square(0, 0, 200)],
                    low_point_roi=[_square(20, 20, 50)],
                    lane_threshold_line=[[10, 10], [190, 190]])
    cache = RoiMaskCache(roi)
    cache.ensure(100, 100)
    # 저지대: 50x50 -> 절반 크기 -> 25x25 (근사, fillPoly 경계 오차 감안)
    assert 550 <= cache.low_area <= 750
    # 차선 기준선도 좌표가 절반으로 줄어야 한다(마스크가 아니라 좌표 그대로)
    assert cache.scaled.lane_threshold_line == [[5, 5], [95, 95]]


def test_set_roi로_교체하면_다음_ensure에서_다시_보정한다():
    roi_a = RoiConfig(frame_width=200, frame_height=200,
                      road_roi=[_square(20, 20, 50)])
    cache = RoiMaskCache(roi_a)
    cache.ensure(100, 100)
    area_a = cache.road_area

    roi_b = RoiConfig(frame_width=200, frame_height=200,
                      road_roi=[_square(20, 20, 100)])  # 2배 큰 정사각형
    cache.set_roi(roi_b)
    cache.ensure(100, 100)
    assert cache.road_area != area_a
    assert 2400 <= cache.road_area <= 2700  # (100*0.5)^2 근사


# --- FloodMetricsEngine / StandaloneFloodMetricsEngine 통합 -----------------

def test_flood_metrics_engine이_스케일된_low_point_roi로_사람을_판정한다():
    """640x360 처리 프레임인데 ROI가 1280x720에 그려져 있으면, 저지대
    ROI 안의 (100,100)에 서 있는 사람(원본 좌표 (200,200)이 절반 스케일된
    좌표)이 위험군으로 잡혀야 한다 — 원본 좌표(스케일 전)로 판정했다면
    화면 밖으로 나가 절대 안 잡힌다."""
    roi = RoiConfig(frame_width=1280, frame_height=720,
                    road_roi=[_square(0, 0, 1280)],
                    low_point_roi=[_square(50, 50, 300)])  # (50,50)-(350,350)
    engine = FloodMetricsEngine(roi=roi)
    mask = np.zeros((360, 640), dtype=np.uint8)  # 절반 해상도
    water = _water(mask)
    # 저지대 원본 (50,50)-(350,350) -> 640x360 스케일 시 (25,25)-(175,175)
    m = engine.update(water, vehicles=[], person_points=[(100.0, 100.0)],
                      frame_number=0, timestamp_sec=0.0)
    assert m.persons_in_danger == 1


def test_standalone_engine도_동일하게_스케일된_low_point_roi를_쓴다():
    from types import SimpleNamespace

    roi = RoiConfig(frame_width=1280, frame_height=720,
                    road_roi=[_square(0, 0, 1280)],
                    low_point_roi=[_square(50, 50, 300)])
    engine = StandaloneMetricsEngine(roi=roi)
    mask = np.zeros((360, 640), dtype=np.uint8)
    water = _water(mask)
    person = SimpleNamespace(bottom_center=(100.0, 100.0))
    detection = SimpleNamespace(vehicles=[], persons=[person])
    m = engine.update(water, detection, frame_number=0, timestamp_sec=0.0)
    assert m.persons_in_danger == 1
