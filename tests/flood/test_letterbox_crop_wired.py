# -*- coding: utf-8 -*-
"""회귀 — 실시간 침수 추론이 레터박스(검은 여백)를 잘라낸 뒤 세그멘테이션한다
(2026-08-28, 「4대탐지기능 성능개선 로드맵」 2단계).

## 왜 이 시험이 있나

``crop_letterbox()``/``detect_letterbox()``는 진작에 구현돼 있었지만
실시간 침수 경로(``runner.py::_update_flood`` → ``segment_water``/
``segment_water_tv``)에는 연결돼 있지 않았다(2026-08-25 기술점검에서
지적한 미해결 약점 — 레터박스 오탐 17.9%). 이 시험은 ``core/
water_segmentation.py::segment_without_letterbox()``가 ①레터박스가
없으면 원본을 그대로 넘기고 ②레터박스가 있으면 잘라낸 영역만 추론하되
**마스크는 원본 좌표계로 되돌려 받는지**를 고정한다 — 좌표를 안 되돌리면
ROI 폴리곤·증거 상자(``mask_bbox()``)가 이 저장소가 반복해 겪은 해상도·
좌표 불일치 함정에 다시 걸린다.
"""
from __future__ import annotations

import numpy as np

from tot_dashboard.flood.water_segmentation import (
    WaterResult,
    segment_without_letterbox,
)


def _frame_no_letterbox(h=60, w=80) -> np.ndarray:
    """레터박스 없음 — 전체가 밝은 노이즈."""
    rng = np.random.default_rng(0)
    return rng.integers(60, 255, (h, w, 3), dtype=np.uint8)


def _frame_with_letterbox(h=100, w=100, band=20) -> np.ndarray:
    """위아래에 순검정 띠(레터박스)가 있는 프레임."""
    rng = np.random.default_rng(1)
    frame = rng.integers(60, 255, (h, w, 3), dtype=np.uint8)
    frame[:band, :, :] = 0          # 위쪽 여백
    frame[h - band:, :, :] = 0      # 아래쪽 여백
    return frame


def test_레터박스_없으면_원본_그대로_추론한다():
    frame = _frame_no_letterbox()
    calls = []

    def fake_segment(f):
        calls.append(f)
        mask = np.zeros(f.shape[:2], dtype=np.uint8)
        return WaterResult(mask=mask, num_instances=0, water_pixels=0,
                           max_confidence=0.0)

    result = segment_without_letterbox(fake_segment, frame)

    assert len(calls) == 1
    assert calls[0].shape == frame.shape, "레터박스가 없으면 잘라내면 안 된다"
    assert result.mask.shape == frame.shape[:2]


def test_레터박스가_있으면_잘라낸_영역만_추론하고_마스크는_원본_좌표로_되돌린다():
    h, w, band = 100, 100, 20
    frame = _frame_with_letterbox(h, w, band)

    def fake_segment(f):
        # 잘라낸 영역 전체를 "물"로 판정하는 가짜 모델
        assert f.shape[0] == h - 2 * band, "여백만큼 안 잘렸다"
        mask = np.full(f.shape[:2], 255, dtype=np.uint8)
        return WaterResult(mask=mask, num_instances=1,
                           water_pixels=int(mask.size), max_confidence=0.9)

    result = segment_without_letterbox(fake_segment, frame)

    assert result.mask.shape == (h, w), "반환 마스크는 원본 프레임 크기여야 한다"
    # ★ 좌표 되돌리기 확인 — 잘라낸 영역(band~h-band)만 물이어야 하고,
    #   여백 자리(위/아래 band픽셀)는 절대 물로 표시되면 안 된다
    #   (여백을 물로 오인하는 문제 자체를 이 시험이 재현/고정한다).
    assert (result.mask[:band, :] == 0).all(), "위쪽 여백이 물로 표시됐다"
    assert (result.mask[h - band:, :] == 0).all(), "아래쪽 여백이 물로 표시됐다"
    assert (result.mask[band:h - band, :] == 255).all(), "잘라낸 영역 판정이 안 옮겨졌다"
    assert result.water_pixels == (h - 2 * band) * w
    assert result.num_instances == 1
    assert result.max_confidence == 0.9


def test_runner가_torchvision_백엔드에서도_letterbox_제거를_거친다(monkeypatch):
    """실제 배선 지점(``runner.py::_update_flood``) 회귀 — 두 백엔드
    (ultralytics·torchvision) 분기 모두 ``segment_without_letterbox``를
    거치는지 직접 확인한다. 여기서 몽키패치가 안 걸리면(=배선이 빠지면)
    아래 프레임 크기 검증이 실패해 알려준다."""
    from tot_dashboard.common.notifier import AlertNotifier
    from tot_dashboard.flood.flood_metrics_engine import FloodMetricsEngine
    from tot_dashboard.flood.risk_engine import RiskEngine, RiskPredictor
    from tot_dashboard.models import TrafficMetrics, TrafficState
    from tot_dashboard.service.runner import PipelineRunner

    # ★ 여백 비율을 낮게 잡는다(15% 미만) — 너무 크면 별도 검사인
    #   `is_likely_corrupted_frame()`(깨진 프레임 방어, 균일 영역 20%
    #   초과 시 프레임 자체를 건너뜀)가 먼저 걸려 이 시험이 확인하려는
    #   지점(segment_water_tv 호출)에 도달하지 못한다 — 실제 운영에서도
    #   레터박스가 과도하면 같은 이유로 프레임이 통째로 스킵될 수 있다는
    #   뜻이므로, 이 시험은 정상 범위의 레터박스만 겨냥한다.
    h, w, band = 200, 100, 15
    frame = _frame_with_letterbox(h, w, band)
    seen_shapes = []

    def fake_segment_water_tv(model, f, conf=0.5):
        seen_shapes.append(f.shape[:2])
        mask = np.zeros(f.shape[:2], dtype=np.uint8)
        return WaterResult(mask=mask, num_instances=0, water_pixels=0,
                           max_confidence=0.0)

    monkeypatch.setattr("tot_dashboard.flood.water_segmentation_tv.segment_water_tv",
                        fake_segment_water_tv)

    runner = object.__new__(PipelineRunner)
    runner.water_cfg = {"process_every_seconds": 0.0}
    runner._water_backend_effective = "torchvision"
    runner._water_tv_conf = 0.5
    runner._water_device = "cpu"
    runner._notifier = AlertNotifier()

    risk_engine = RiskEngine({}, {})
    c = {
        "water_model": object(),
        "last_water_t": -999.0,
        "frame_no": 0,
        "corrupted_skips": 0,
        "flood_engine": FloodMetricsEngine(),
        "risk_engine": risk_engine,
        "risk_predictor": RiskPredictor(risk_engine, {}),
        "block": {"id": "TEST-LETTERBOX-WIRED", "name": "시험지점"},
    }

    class _FakePS:
        def __init__(self):
            self.vehicles = []
            self.persons = []
            self.metrics = TrafficMetrics(
                t_sec=0.0, n_vehicles=0, mean_speed=0.0, speed_drop=0.0,
                density=0.0, queue_len=0, stalled=0, state=TrafficState.free,
            )

    runner._update_flood(c, 1.0, frame, _FakePS())

    assert seen_shapes, "segment_water_tv가 호출되지 않았다"
    assert seen_shapes[0] == (h - 2 * band, w), (
        "실제 추론에 넘어간 프레임이 여백 제거를 거치지 않았다")
