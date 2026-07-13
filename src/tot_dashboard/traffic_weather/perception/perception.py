"""Perception — per-tick object-attribute update layer.

Ported from flood3's ``perception/perception.py``. Each tick:
  - CCTV (YOLO+ByteTrack) -> updates per-vehicle attributes (speed, speed_drop,
    age, stalled)
  - rainfall provider -> updates the scene's weather attributes (rain_mm_h,
    rising/falling trend)
-> bundles both into a ``PerceptionState`` passed to the later stages.
"""
from __future__ import annotations

from ...models import PerceptionState, WeatherState
from .detection_source import PERSON_CLASSES
from .traffic_tracker import TrafficBehaviorTracker


def _bottom_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, _, x2, y2 = bbox
    return ((x1 + x2) / 2.0, y2)


def _with_trend(w: WeatherState, prev: float | None, eps: float = 0.1) -> WeatherState:
    """Attach a rainfall increasing/decreasing trend (weather attribute)."""
    if prev is None:
        return w
    d = round(w.rain_mm_h - prev, 2)
    trend = "증가" if d > eps else ("감소" if d < -eps else "유지")
    return w.model_copy(update={"delta_mm_h": d, "trend": trend})


class Perception:
    def __init__(self, block: dict, rainfall, fps: float = 5.0,
                 baseline_hint: float | None = None, use_bytetrack: bool = True):
        self.block = block
        self.rainfall = rainfall
        self.tracker = TrafficBehaviorTracker(
            fps=fps, baseline_hint=baseline_hint, use_bytetrack=use_bytetrack)
        self._prev_rain: float | None = None

    def step(self, t: float, detections, frame_wh=None) -> PerceptionState:
        # weather object attribute: rainfall + trend
        weather = _with_trend(self.rainfall.at(t), self._prev_rain)
        self._prev_rain = weather.rain_mm_h
        # split vehicles/persons — tracking (ByteTrack) applies to vehicles
        # only; persons only need a per-frame bbox bottom-center for the
        # flood-risk "person" signal (no track needed)
        vehicle_dets = [d for d in detections if d.cls not in PERSON_CLASSES]
        person_pts = [_bottom_center(d.bbox) for d in detections if d.cls in PERSON_CLASSES]
        # vehicle object attributes: speed/drop (tracker updates last_objects)
        metrics = self.tracker.update(vehicle_dets, t, frame_wh=frame_wh)
        return PerceptionState(
            t_sec=round(t, 2), block_id=self.block.get("id", ""),
            vehicles=self.tracker.last_objects, persons=person_pts,
            weather=weather, metrics=metrics)
