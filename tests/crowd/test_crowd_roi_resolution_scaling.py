# -*- coding: utf-8 -*-
"""인파(crowd) ROI 저장 해상도 ≠ 실제 분석 해상도 보정 — 2026-08-23 신설.

침수 도메인(SEOUL-1042)에서 발견된 것과 같은 근본 원인이 인파관리에도
있었다: ``analysis_roi``/``intrusion_roi``/``loiter_roi``는 ROI를 그린
정지영상 해상도(``core/cameras.py::to_block_dict()``가 실어 주는
``roi_frame_width``/``roi_frame_height``) 기준 좌표인데,
``CrowdLiveAnalyzer``는 그 좌표를 자신의 처리 해상도(``width``/``height``)에
보정 없이 그대로 썼다. traffic_tracker.py/road/live_analyzer.py가 이미
쓰는 ``common.roi.scale_polygons()``로 통일한다.
"""
from __future__ import annotations

from tot_dashboard.crowd.live_analyzer import CrowdLiveAnalyzer


def _square(x, y, size):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def test_해상도가_다르면_analysis_roi가_비례_보정된다():
    cfg = {
        "roi_frame_width": 1280, "roi_frame_height": 720,
        "analysis_roi": [_square(100, 100, 400)],
    }
    a = CrowdLiveAnalyzer(cfg=cfg, block_id="TEST", node_id="CAM",
                          width=640, height=360)  # 절반 해상도
    assert a.analysis_roi is not None
    poly = a.analysis_roi[0]
    assert all(0 <= x <= 640 and 0 <= y <= 360 for x, y in poly)
    assert poly[0] == [50, 50]  # (100,100) * 0.5


def test_해상도가_같으면_원본_그대로_쓴다():
    cfg = {
        "roi_frame_width": 640, "roi_frame_height": 360,
        "analysis_roi": [_square(100, 100, 400)],
        "intrusion_roi": [_square(0, 0, 200)],
        "loiter_roi": [_square(0, 0, 200)],
    }
    a = CrowdLiveAnalyzer(cfg=cfg, block_id="TEST", node_id="CAM",
                          width=640, height=360)
    assert a.analysis_roi == cfg["analysis_roi"]
    assert a.events_detector.intrusion_roi == cfg["intrusion_roi"]
    assert a.events_detector.loiter_roi == cfg["loiter_roi"]


def test_저장_해상도_정보가_없으면_보정하지_않는다():
    """레거시 설정(roi_frame_width 없음) — 원본 그대로 써야 기존 동작이
    깨지지 않는다(scale_polygons 의 안전한 폴백과 같은 원칙)."""
    cfg = {"analysis_roi": [_square(100, 100, 400)]}
    a = CrowdLiveAnalyzer(cfg=cfg, block_id="TEST", node_id="CAM",
                          width=640, height=360)
    assert a.analysis_roi == cfg["analysis_roi"]


def test_intrusion_roi와_loiter_roi도_함께_보정된다():
    cfg = {
        "roi_frame_width": 1280, "roi_frame_height": 720,
        "intrusion_roi": [_square(100, 100, 400)],
        "loiter_roi": [_square(200, 200, 200)],
    }
    a = CrowdLiveAnalyzer(cfg=cfg, block_id="TEST", node_id="CAM",
                          width=640, height=360)
    assert a.events_detector.intrusion_roi[0][0] == [50, 50]
    assert a.events_detector.loiter_roi[0][0] == [100, 100]
