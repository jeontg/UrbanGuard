"""Canonical flood-metrics dataclass + algorithms shared by both flood entry
points.

The integration review (docs/integration_plan.md section 3-1) found that
underpath_flood_dashboard's ``MetricsEngine`` (self-contained: does its own
object detection/tracking) and flood3's ``FloodMetricsEngine`` (externally
fed: receives already-tracked vehicles/traffic from a separate perception
step) are NOT simple duplicates — they sit at different points in two
different pipeline architectures and cannot be merged into one `update()`
signature. What *is* identical between them (confirmed by direct code diff) is
the actual math: ROI-mask intersection, tire-zone submersion analysis, and the
expansion-rate trend calculation. Those pieces are extracted here so both
``flood/standalone_pipeline.py`` and ``flood/flood_metrics_engine.py`` share
one implementation instead of two copies that could silently drift apart.

``FloodMetrics`` below is the single canonical output dataclass both entry
points return — the union of underpath_flood_dashboard's ``FrameMetrics`` and
flood3's ``FloodMetrics``. Fields that only one of the two original engines
populated (e.g. ``avg_vehicle_pixel_speed``, only computed by the
self-contained standalone engine) simply keep their default when the other
entry point is used.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from ..common.roi import RoiConfig, PolygonList, polygon_mask, scale_polygons
from ..models import TrafficState


@dataclass
class FloodMetrics:
    """순수 침수 지표만 담는다 (2026-08-21, flood/traffic 도메인 분리).

    ⚠️ 예전에는 이 클래스가 ``traffic_state``/``stopped_vehicles_near_water``도
    들고 있었다. `traffic_state`는 순수 교통 신호라 flood가 소유할 이유가
    없고, `stopped_vehicles_near_water`는 "정지(교통 행동)"+"물 근처(위치)"
    결합값이라 어느 한쪽의 raw 신호도 아니다 — 필요하면 orchestrator
    (``service/runner.py``)가 flood/traffic 두 산출물을 조합해 계산한다.
    S-23 오프라인 파이프라인(``standalone_pipeline.py``)은 지금도 이 필드를
    직접 쓰므로, 그 경로만 아래 :class:`StandaloneFloodMetrics`를 쓴다.
    """
    frame_number: int = 0
    timestamp_sec: float = 0.0

    # water
    water_area_pixels: int = 0
    road_roi_area_pixels: int = 0
    water_area_ratio: float = 0.0
    water_expansion_rate: float = 0.0
    water_crosses_lane: bool = False
    water_near_low_point: bool = False
    low_point_water_ratio: float = 0.0
    roi_defined: bool = False

    # objects (순수 관측값 — 추적 좌표일 뿐 "교통 도메인 소유"로 보지 않는다)
    person_count: int = 0
    vehicle_count: int = 0
    recent_person_count: int = 0  # standalone-pipeline-only diagnostic (peak in recent window)
    recent_vehicle_count: int = 0  # standalone-pipeline-only diagnostic
    avg_vehicle_pixel_speed: float | None = None  # standalone-pipeline-only diagnostic

    # 침수의 직접 물증 — 차량 bbox와 물 마스크의 순수 기하 교차 (교통 행동과 무관)
    vehicles_touching_water: int = 0
    persons_in_danger: int = 0
    vehicles_tire_in_water: int = 0
    max_vehicle_submersion: float = 0.0

    # filled later by alert_engine.py (discrete 1..5 level, underpath_flood_dashboard feature)
    alert_level: int = 1
    alert_reason: str = ""

    # filled later by risk_engine.py (continuous 0..100 score) + predictor
    risk_score: float = 0.0
    risk_grade: int = 1
    risk_trend: str = "insufficient"
    pred_water_ratio_10s: float = 0.0
    pred_risk_10s: float = 0.0
    eta_danger_sec: float = -1.0

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StandaloneFloodMetrics(FloodMetrics):
    """S-23 오프라인 파이프라인(``standalone_pipeline.py``) 전용.

    실시간 경로(``FloodMetricsEngine``)는 이 서브클래스를 절대 쓰지 않는다 —
    그 경로는 이제 순수 ``FloodMetrics``만 다룬다. S-23은 자체 YOLO로 교통
    상태까지 계산하는 완전 자립형 파이프라인이라, 이번 도메인 분리 범위에서는
    제외하고 옛 필드만 여기로 격리해 영향을 차단한다(2026-08-21).
    """
    traffic_state: TrafficState = TrafficState.free
    stopped_vehicles_near_water: int = 0

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["traffic_state"] = self.traffic_state.value
        return row


class RoiMaskCache:
    """Rebuilds road/low-point ROI masks only when the frame shape changes.

    Ported from the (identical) ``_ensure_masks`` logic in both
    underpath_flood_dashboard's ``MetricsEngine`` and flood3's
    ``FloodMetricsEngine``.
    """

    def __init__(self, roi: RoiConfig | None = None) -> None:
        self.roi = roi or RoiConfig()
        # ROI 저장 당시 해상도로 스케일 보정된 사본. `ensure()`가 실제
        # 판정 프레임 크기를 알게 되는 순간 채워진다 — 그 전까지는 원본과
        # 같다(호출 순서에 안전하도록).
        self.scaled: RoiConfig = self.roi
        self._shape: tuple[int, int] | None = None
        self.road_mask: np.ndarray | None = None
        self.low_mask: np.ndarray | None = None
        self.road_area: int = 0
        self.low_area: int = 0

    def set_roi(self, roi: RoiConfig) -> None:
        self.roi = roi
        self.scaled = roi
        self._shape = None  # force rebuild

    def ensure(self, h: int, w: int) -> None:
        if self._shape == (h, w):
            return
        self._shape = (h, w)
        # ⚠️ 2026-08-23 실사용 중 발견 — ROI 는 **저장 당시 정지영상
        # 해상도** 기준 픽셀 좌표인데, 이 프레임(물 세그멘테이션 마스크의
        # 실제 해상도)이 그것과 다르면(스트림 화질이 바뀌었거나, 캘리브레이션
        # 때와 다른 소스로 잡았거나) 좌표를 그대로 쓰면 도로가 아닌 엉뚱한
        # 자리를 도로 ROI로 착각한다 — 침수 위험도 자체가 조용히 틀어진다.
        # traffic_tracker.py/road/live_analyzer.py 가 쓰는 것과 같은
        # scale_polygons() 로 매 해상도 변경 시 한 번만 보정한다.
        from_wh = (self.roi.frame_width, self.roi.frame_height)
        to_wh = (w, h)
        self.scaled = RoiConfig(
            camera_name=self.roi.camera_name, frame_width=w, frame_height=h,
            road_roi=scale_polygons(self.roi.road_roi, from_wh, to_wh),
            low_point_roi=scale_polygons(self.roi.low_point_roi, from_wh, to_wh),
            lane_threshold_line=(
                scale_polygons([self.roi.lane_threshold_line], from_wh, to_wh)[0]
                if self.roi.has_lane_line else []),
        )
        if self.scaled.has_road:
            self.road_mask = polygon_mask(self.scaled.road_roi, h, w)
            self.road_area = int(np.count_nonzero(self.road_mask))
        else:
            self.road_mask = None
            self.road_area = h * w
        if self.scaled.has_low_point:
            self.low_mask = polygon_mask(self.scaled.low_point_roi, h, w)
            self.low_area = int(np.count_nonzero(self.low_mask))
        else:
            self.low_mask = None
            self.low_area = 0


class ExpansionRateTracker:
    """Water-ratio expansion trend (ratio change per second) over a rolling
    window. Identical logic in both original engines' ``_expansion_rate``.
    """

    def __init__(self, window_seconds: float = 3.0) -> None:
        self.window_seconds = window_seconds
        self._hist: deque[tuple[float, float]] = deque()

    def update(self, t: float, ratio: float) -> float:
        self._hist.append((t, ratio))
        while self._hist and t - self._hist[0][0] > self.window_seconds:
            if len(self._hist) <= 1:
                break
            self._hist.popleft()
        if len(self._hist) < 2:
            return 0.0
        t0, r0 = self._hist[0]
        dt = t - t0
        return (ratio - r0) / dt if dt > 1e-6 else 0.0


def point_on_mask(point: tuple[float, float], mask: np.ndarray, pad: int = 0) -> bool:
    h, w = mask.shape[:2]
    x, y = int(round(point[0])), int(round(point[1]))
    if pad <= 0:
        return 0 <= x < w and 0 <= y < h and mask[y, x] > 0
    x0, x1 = max(0, x - pad), min(w, x + pad + 1)
    y0, y1 = max(0, y - pad), min(h, y + pad + 1)
    if x0 >= x1 or y0 >= y1:
        return False
    return bool(np.any(mask[y0:y1, x0:x1] > 0))


def count_points_on_mask(points, mask: np.ndarray, pad: int = 0) -> int:
    return sum(1 for pt in points if point_on_mask(pt, mask, pad))


def tire_zone_analysis(bboxes: list[tuple[float, float, float, float]],
                        water_mask: np.ndarray) -> tuple[int, float]:
    """Water contact at each box's tire zone (bottom 20% band) + worst-case
    submersion depth (fraction of box height covered by water, measured
    upward from the bottom). Identical logic in both original engines'
    ``_tire_zone_analysis`` — generalized here to take plain bbox tuples so it
    works whether the caller has underpath_flood_dashboard-style ``Detection``
    objects or flood3-style ``VehicleObject`` objects (both expose an
    ``(x1, y1, x2, y2)`` box, just under different attribute names).
    """
    h, w = water_mask.shape[:2]
    count = 0
    max_sub = 0.0
    for box in bboxes:
        x1, y1, x2, y2 = (int(round(c)) for c in box)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        box_h = y2 - y1
        if x2 <= x1 or box_h <= 0:
            continue
        band = max(3, int(0.20 * box_h))
        tire_region = water_mask[y2 - band:y2, x1:x2]
        if tire_region.size and np.any(tire_region > 0):
            count += 1
        box_region = water_mask[y1:y2, x1:x2] > 0
        if box_region.size:
            row_cov = box_region.mean(axis=1)
            submerged = row_cov >= 0.30
            sub_rows = 0
            for r in range(box_region.shape[0] - 1, -1, -1):  # bottom -> up
                if submerged[r]:
                    sub_rows += 1
                else:
                    break
            max_sub = max(max_sub, sub_rows / box_region.shape[0])
    return count, float(max_sub)


def bottom_center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2.0, y2)
