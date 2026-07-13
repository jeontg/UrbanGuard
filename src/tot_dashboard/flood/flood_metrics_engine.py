"""Externally-fed flood metrics engine — for the live multi-block server path.

Ported from flood3's ``flood/flood_metrics.py``. Unlike
``standalone_pipeline.StandaloneMetricsEngine`` (which does its own object
detection/tracking), this engine receives already-tracked vehicles, person
points, and traffic metrics from an external perception step (flood3's
``perception.Perception`` / ``perception.traffic_tracker.TrafficBehaviorTracker``,
ported in Phase 3) — this is what lets it run per-tick inside a multi-block
server loop without re-running detection. See docs/integration_plan.md
section 3-1 for why this is kept as its own class rather than merged with the
standalone engine.

Also applies an EMA smoothing pass on water_area_ratio (flood3 addition, not
present in underpath_flood_dashboard) to damp single-frame flicker from
nighttime reflections/headlights on live HLS streams — the same pattern
flood3's ``TrafficBehaviorTracker._drop_ema`` uses for speed_drop.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ..common.roi import RoiConfig, point_in_polygons, water_crosses_line
from .metrics_core import (
    FloodMetrics,
    RoiMaskCache,
    ExpansionRateTracker,
    bottom_center,
    count_points_on_mask,
    point_on_mask,
    tire_zone_analysis,
)
from .water_segmentation import WaterResult

if TYPE_CHECKING:
    from ..models import TrafficMetrics, VehicleObject


class FloodMetricsEngine:
    """One instance per block (camera); call :meth:`update` once per tick."""

    def __init__(self, roi: RoiConfig | None = None,
                 expansion_window_seconds: float = 3.0,
                 ema_alpha: float = 0.5) -> None:
        self.roi_cache = RoiMaskCache(roi)
        self._expansion = ExpansionRateTracker(window_seconds=expansion_window_seconds)
        # water_area_ratio EMA smoothing — alpha=1.0 disables smoothing (raw value).
        self.ema_alpha = ema_alpha
        self._ratio_ema: float | None = None

    def set_roi(self, roi: RoiConfig) -> None:
        self.roi_cache.set_roi(roi)
        self._ratio_ema = None  # ROI basis changed -> reset smoothing history too

    def update(
        self,
        water: WaterResult,
        vehicles: list["VehicleObject"],
        person_points: list[tuple[float, float]],
        traffic: "TrafficMetrics",
        frame_number: int,
        timestamp_sec: float,
    ) -> FloodMetrics:
        h, w = water.mask.shape[:2]
        self.roi_cache.ensure(h, w)
        roi = self.roi_cache.roi

        m = FloodMetrics(frame_number=frame_number, timestamp_sec=float(timestamp_sec))
        m.roi_defined = roi.has_road
        m.traffic_state = traffic.state

        if self.roi_cache.road_mask is not None:
            water_in_road = int(np.count_nonzero(
                (water.mask > 0) & (self.roi_cache.road_mask > 0)))
        else:
            water_in_road = int(water.water_pixels)
        m.water_area_pixels = water_in_road
        m.road_roi_area_pixels = int(self.roi_cache.road_area)
        raw_ratio = (water_in_road / self.roi_cache.road_area) if self.roi_cache.road_area else 0.0
        self._ratio_ema = raw_ratio if self._ratio_ema is None \
            else self.ema_alpha * raw_ratio + (1 - self.ema_alpha) * self._ratio_ema
        m.water_area_ratio = self._ratio_ema

        m.water_expansion_rate = self._expansion.update(timestamp_sec, m.water_area_ratio)

        if roi.has_lane_line:
            m.water_crosses_lane = water_crosses_line(water.mask, roi.lane_threshold_line)

        if self.roi_cache.low_mask is not None and self.roi_cache.low_area > 0:
            low_water = int(np.count_nonzero(
                (water.mask > 0) & (self.roi_cache.low_mask > 0)))
            m.low_point_water_ratio = low_water / self.roi_cache.low_area
            m.water_near_low_point = m.low_point_water_ratio > 0.01

        m.person_count = len(person_points)
        persons_in_water = count_points_on_mask(person_points, water.mask)
        persons_in_low = 0
        if roi.has_low_point:
            persons_in_low = sum(
                1 for p in person_points if point_in_polygons(p, roi.low_point_roi)
            )
        m.persons_in_danger = max(persons_in_water, persons_in_low)

        m.vehicles_touching_water = count_points_on_mask(
            [bottom_center(v.bbox) for v in vehicles], water.mask
        )
        m.stopped_vehicles_near_water = sum(
            1 for v in vehicles
            if v.stalled and point_on_mask(bottom_center(v.bbox), water.mask, pad=8)
        )
        m.vehicles_tire_in_water, m.max_vehicle_submersion = tire_zone_analysis(
            [v.bbox for v in vehicles], water.mask
        )

        return m
