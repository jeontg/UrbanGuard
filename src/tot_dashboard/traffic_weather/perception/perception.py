"""Perception — per-tick object-attribute update layer.

Ported from flood3's ``perception/perception.py``. Each tick:
  - CCTV (YOLO+ByteTrack) -> updates per-vehicle attributes (speed, speed_drop,
    age, stalled)
  - rainfall provider -> updates the scene's weather attributes (rain_mm_h,
    rising/falling trend)
-> bundles both into a ``PerceptionState`` passed to the later stages.
"""
from __future__ import annotations

from ...core.calibration import from_dict as _calibration_from_dict
from ...models import PerceptionState, TrafficState, WeatherState
from .detection_source import PERSON_CLASSES
from .incident_events import TrafficIncidentDetector
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
        # ★ 2026-08-22 — S-80/S-81 에서 지정한 교통위험 ROI 를 판정기에
        #   넘긴다. to_block_dict() 가 block["traffic"] 에 실어 주므로
        #   여기서 꺼내 쓰기만 하면 된다(ROI 미설정이면 빈 값 → 예전처럼
        #   화면 전체 기준으로 판정).
        _tcfg = block.get("traffic") or {}
        _roi_wh = (_tcfg.get("roi_frame_width"), _tcfg.get("roi_frame_height"))
        # ★ 2026-08-26 — to_block_dict() 가 sub_row.config 전체를 복사해
        #   넘기므로 "calibration" 키(core.calibration.CALIBRATION_KEY)도
        #   이미 block["traffic"] 안에 있다. 없으면 from_dict(None) 이
        #   빈 Calibration() 을 돌려줘 지금까지와 동일하게 동작한다
        #   (km/h 는 전부 None — "가짜 값 대신 미보정 표시").
        self.tracker = TrafficBehaviorTracker(
            fps=fps, baseline_hint=baseline_hint, use_bytetrack=use_bytetrack,
            congestion_roi=_tcfg.get("congestion_roi") or [],
            roi_frame_wh=_roi_wh,
            cal=_calibration_from_dict(_tcfg.get("calibration")))
        # ★ 2026-08-26 — 돌발상황 탐지기. 보행자 판정은 새 도형을 강제하지
        #   않고 congestion_roi(정체 감시 구역)를 그대로 재사용한다 —
        #   차량 흐름을 재는 도로 영역이 곧 "보행자가 있으면 안 되는
        #   영역"이기도 하다.
        self.incidents = TrafficIncidentDetector(
            pedestrian_roi=_tcfg.get("congestion_roi") or [],
            pedestrian_exempt_roi=_tcfg.get("pedestrian_exempt_roi") or [],
            roi_frame_wh=_roi_wh, block_id=block.get("id", ""),
            # ★ 2026-08-26 — 역주행(Phase 4). 화살표가 없으면 판정기가
            # 스스로 판정을 건너뛴다(incident_events.py 참고) — 여기서는
            # 있는 그대로 넘기기만 하면 된다.
            flow_arrows=_tcfg.get("flow_arrows") or [])
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
        # 돌발상황 — 트래커는 건드리지 않고 자기 상태로 별도 판정한다.
        # is_blocked: 사고 의심(Phase 5)이 정체와 사고를 구분하는 데 쓴다
        # — 도로 전체가 정지 상태면 그건 정체이지 사고가 아니다.
        self.incidents.update(t, self.tracker.last_objects, person_pts, frame_wh,
                              is_blocked=(metrics.state == TrafficState.blocked))
        return PerceptionState(
            t_sec=round(t, 2), block_id=self.block.get("id", ""),
            vehicles=self.tracker.last_objects, persons=person_pts,
            weather=weather, metrics=metrics,
            incidents=[i.to_dict() for i in self.incidents.active(t)])
