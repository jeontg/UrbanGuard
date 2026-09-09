"""Self-contained, single-camera flood pipeline (CLI / offline batch analysis).

Ported from underpath_flood_dashboard's ``src/metrics.py`` (the
``MetricsEngine`` half — object-detection/tracking/traffic-state logic) and
``src/video_processor.py`` (``Pipeline``/``FrameResult``/``process_run``).

This is deliberately kept as its own, self-contained entry point rather than
merged into flood3-style externally-fed processing (see
``flood_metrics_engine.py`` and docs/integration_plan.md section 3-1): this
pipeline does its own object detection AND tracking in one YOLO call
(``object_detection.detect_objects``), whereas the flood3-style engine
receives already-tracked vehicles from a separate perception step. Trying to
give both the same ``update()`` signature breaks one or the other.

Shared math (ROI-mask intersection, tire-zone submersion, expansion rate) is
factored into ``metrics_core.py`` so this file and ``flood_metrics_engine.py``
can't silently drift apart on the parts that ARE identical.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

from ..common.models_loader import ModelBundle, load_models
from ..common.roi import RoiConfig, load_roi_config, point_in_polygons, water_crosses_line
from ..common.video_io import detect_letterbox, get_preview_frame, iter_source, source_name
from ..models import TrafficState
from . import visualization as viz
from .alert_engine import AlertEngine
from .config import load_alert_config, load_model_config, load_risk_config
from .metrics_core import (
    FloodMetrics,
    StandaloneFloodMetrics,
    RoiMaskCache,
    ExpansionRateTracker,
    count_points_on_mask,
    point_on_mask,
    tire_zone_analysis,
)
from .object_detection import DetectionResult, detect_objects
from .risk_engine import PredictionResult, RiskEngine, RiskPredictor, RiskResult, write_back
from .water_segmentation import WaterResult, segment_water


class StandaloneMetricsEngine:
    """Computes ``FloodMetrics`` directly from raw object detections — does
    its own vehicle-speed / traffic-state / recent-count bookkeeping (the
    parts flood3's externally-fed ``FloodMetricsEngine`` does NOT do, because
    that responsibility lives in flood3's separate ``TrafficBehaviorTracker``
    instead). See ``alert_engine.py``'s module docstring for the
    6-state -> ``TrafficState`` mapping used by :meth:`_traffic_state`.
    """

    def __init__(
        self,
        roi: RoiConfig | None = None,
        params: dict[str, Any] | None = None,
        recent_window_seconds: float = 5.0,
        expansion_window_seconds: float = 3.0,
    ) -> None:
        self.roi_cache = RoiMaskCache(roi)
        p = params or {}
        self.stopped_speed_px = float(p.get("stopped_speed_px", 2.0))
        self.slow_speed_px = float(p.get("slow_speed_px", 8.0))
        self.jam_min_vehicles = int(p.get("jam_min_vehicles", 3))
        self.recent_window_seconds = recent_window_seconds
        self._expansion = ExpansionRateTracker(window_seconds=expansion_window_seconds)
        self._count_hist: deque[tuple[float, int, int]] = deque()
        self._track_hist: dict[int, tuple[float, float, float]] = {}

    def set_roi(self, roi: RoiConfig) -> None:
        self.roi_cache.set_roi(roi)

    def update(
        self,
        water: WaterResult,
        detection: DetectionResult,
        frame_number: int,
        timestamp_sec: float,
    ) -> StandaloneFloodMetrics:
        h, w = water.mask.shape[:2]
        self.roi_cache.ensure(h, w)
        # ⚠️ 2026-08-23 — flood_metrics_engine.py 와 같은 이유로 `scaled`
        # (이 프레임 해상도로 보정된 사본)를 써야 한다. 원본(`roi_cache.roi`)
        # 을 쓰면 저장 해상도와 실제 처리 해상도가 다를 때 lane_threshold_line
        # ·low_point_roi 판정만 road/low 마스크와 어긋나게 된다.
        roi = self.roi_cache.scaled

        # ★ 2026-08-21: 이 파이프라인(S-23, 오프라인 자립형 분석)만
        #   StandaloneFloodMetrics를 쓴다 — traffic_state/
        #   stopped_vehicles_near_water는 여기서만 유효하고, 실시간 경로
        #   (FloodMetricsEngine)는 순수 FloodMetrics만 다룬다.
        m = StandaloneFloodMetrics(frame_number=frame_number, timestamp_sec=float(timestamp_sec))
        m.roi_defined = roi.has_road

        if self.roi_cache.road_mask is not None:
            water_in_road = int(np.count_nonzero(
                (water.mask > 0) & (self.roi_cache.road_mask > 0)))
        else:
            water_in_road = int(water.water_pixels)
        m.water_area_pixels = water_in_road
        m.road_roi_area_pixels = int(self.roi_cache.road_area)
        m.water_area_ratio = (water_in_road / self.roi_cache.road_area) \
            if self.roi_cache.road_area else 0.0

        m.water_expansion_rate = self._expansion.update(timestamp_sec, m.water_area_ratio)

        if roi.has_lane_line:
            m.water_crosses_lane = water_crosses_line(water.mask, roi.lane_threshold_line)

        if self.roi_cache.low_mask is not None and self.roi_cache.low_area > 0:
            low_water = int(np.count_nonzero(
                (water.mask > 0) & (self.roi_cache.low_mask > 0)))
            m.low_point_water_ratio = low_water / self.roi_cache.low_area
            m.water_near_low_point = m.low_point_water_ratio > 0.01

        persons = detection.persons
        vehicles = detection.vehicles
        m.person_count = len(persons)
        m.vehicle_count = len(vehicles)
        m.recent_person_count, m.recent_vehicle_count = self._recent_counts(
            timestamp_sec, m.person_count, m.vehicle_count
        )

        speeds, stationary_ids = self._vehicle_speeds(vehicles, timestamp_sec)
        if speeds:
            m.avg_vehicle_pixel_speed = float(np.mean(speeds))
        m.traffic_state = self._traffic_state(m.vehicle_count, m.avg_vehicle_pixel_speed)

        m.vehicles_touching_water = count_points_on_mask(
            [v.bottom_center for v in vehicles], water.mask
        )
        persons_in_water = count_points_on_mask(
            [p.bottom_center for p in persons], water.mask
        )
        persons_in_low = 0
        if roi.has_low_point:
            persons_in_low = sum(
                1 for p in persons if point_in_polygons(p.bottom_center, roi.low_point_roi)
            )
        m.persons_in_danger = max(persons_in_water, persons_in_low) if (
            persons_in_water or persons_in_low
        ) else persons_in_water
        m.stopped_vehicles_near_water = sum(
            1 for v in vehicles
            if v.track_id in stationary_ids
            and point_on_mask(v.bottom_center, water.mask, pad=8)
        )

        m.vehicles_tire_in_water, m.max_vehicle_submersion = tire_zone_analysis(
            [v.box for v in vehicles], water.mask
        )

        return m

    def _recent_counts(self, t: float, persons: int, vehicles: int) -> tuple[int, int]:
        self._count_hist.append((t, persons, vehicles))
        while self._count_hist and t - self._count_hist[0][0] > self.recent_window_seconds:
            self._count_hist.popleft()
        rp = max((c[1] for c in self._count_hist), default=persons)
        rv = max((c[2] for c in self._count_hist), default=vehicles)
        return int(rp), int(rv)

    def _vehicle_speeds(self, vehicles, t: float):
        speeds: list[float] = []
        stationary: set[int] = set()
        for v in vehicles:
            if v.track_id is None:
                continue
            cx, cy = v.center
            prev = self._track_hist.get(v.track_id)
            self._track_hist[v.track_id] = (cx, cy, t)
            if prev is None:
                continue
            px, py, pt = prev
            dt = t - pt
            if dt <= 1e-6:
                continue
            dist = math.hypot(cx - px, cy - py)
            speed = dist / dt
            speeds.append(speed)
            if speed < self.stopped_speed_px:
                stationary.add(v.track_id)
        if len(self._track_hist) > 256:
            self._track_hist = {
                k: val for k, val in self._track_hist.items() if t - val[2] < 10.0
            }
        return speeds, stationary

    def _traffic_state(self, vehicle_count: int, avg_speed: float | None) -> TrafficState:
        if vehicle_count == 0:
            return TrafficState.free  # "empty"
        if avg_speed is None:
            return TrafficState.free  # "unknown" (tracking off) -> no signal
        if avg_speed >= self.slow_speed_px:
            return TrafficState.free  # "normal"
        if avg_speed >= self.stopped_speed_px:
            return TrafficState.slow
        return TrafficState.blocked if vehicle_count >= self.jam_min_vehicles \
            else TrafficState.congested


@dataclass
class FrameResult:
    frame_number: int
    timestamp_sec: float
    frame: np.ndarray
    water: WaterResult
    detection: DetectionResult
    metrics: FloodMetrics
    risk: RiskResult | None = None
    prediction: PredictionResult | None = None

    def annotated_bgr(self, roi: RoiConfig | None) -> np.ndarray:
        return viz.annotate_combined(
            self.frame, self.water, self.detection, roi,
            level=self.metrics.alert_level, reason=self.metrics.alert_reason,
        )

    def risk_bgr(self, roi: RoiConfig | None) -> np.ndarray:
        return viz.draw_risk_overlay(
            self.frame, self.water, self.detection, roi, self.risk, self.prediction,
        )


class Pipeline:
    """Holds models + stateful engines for one single-camera processing session."""

    def __init__(
        self,
        bundle: ModelBundle,
        model_config: dict[str, Any],
        alert_config: dict[str, Any] | None = None,
        roi: RoiConfig | None = None,
        risk_config: dict[str, Any] | None = None,
    ) -> None:
        self.bundle = bundle
        self.mcfg = model_config
        self.acfg = alert_config or load_alert_config()
        self.rcfg = risk_config or load_risk_config()
        self.roi = roi or RoiConfig()
        self.metrics_engine = StandaloneMetricsEngine(roi=self.roi, params=self.acfg)
        self.alert_engine = AlertEngine(self.acfg)
        self.risk_engine = RiskEngine(self.rcfg, self.acfg)
        self.risk_predictor = RiskPredictor(self.risk_engine, self.rcfg)
        self.use_tracking = bool(model_config.get("use_tracking", True))
        self.tracker = model_config.get("tracker", "bytetrack.yaml")

    @classmethod
    def from_config(cls, model_config=None, alert_config=None, roi=None,
                    risk_config=None) -> "Pipeline":
        model_config = model_config or load_model_config()
        bundle = load_models(model_config)
        return cls(bundle, model_config, alert_config, roi, risk_config)

    def set_roi(self, roi: RoiConfig) -> None:
        self.roi = roi
        self.metrics_engine.set_roi(roi)

    def reset_state(self) -> None:
        """Reset temporal/alert/risk/tracker state before a fresh run."""
        self.metrics_engine = StandaloneMetricsEngine(roi=self.roi, params=self.acfg)
        self.alert_engine = AlertEngine(self.acfg)
        self.risk_predictor.reset()
        try:
            self.bundle.object_model.predictor = None  # drop tracker state
        except Exception:
            pass

    def process_frame(
        self, frame: np.ndarray, frame_number: int, timestamp_sec: float,
        track: bool | None = None,
    ) -> FrameResult:
        mcfg = self.mcfg
        water = segment_water(
            self.bundle.water_model, frame,
            conf=mcfg.get("water_conf", 0.10), iou=mcfg.get("iou", 0.5),
            imgsz=mcfg.get("imgsz", 640), device=self.bundle.device,
        )
        det = detect_objects(
            self.bundle.object_model, frame,
            class_ids=self.bundle.target_class_ids,
            person_class_ids=self.bundle.person_class_ids,
            vehicle_class_ids=self.bundle.vehicle_class_ids,
            conf=mcfg.get("object_conf", 0.30), iou=mcfg.get("iou", 0.5),
            imgsz=mcfg.get("imgsz", 640), device=self.bundle.device,
            track=self.use_tracking if track is None else track,
            tracker=self.tracker,
        )
        m = self.metrics_engine.update(water, det, frame_number, timestamp_sec)
        self.alert_engine.update(m)

        risk = self.risk_engine.score(m)
        m.risk_score, m.risk_grade = risk.risk_score, risk.risk_grade
        prediction = self.risk_predictor.update(m)
        write_back(m, risk, prediction)
        return FrameResult(frame_number, timestamp_sec, frame, water, det, m,
                           risk=risk, prediction=prediction)


def _resolve_letterbox_crop(pipeline: "Pipeline", source: dict[str, Any],
                            mode: str) -> tuple[int, int, int, int] | None:
    """레터박스 크롭 박스를 결정한다. 크롭하지 않으면 None.

    왜 필요한가: 물 세그멘테이션 모델은 레터박스(검은 여백) 영상을 학습한 적이
    없어 **검은 띠를 물로 오인**한다(실측 - 기존 Ultralytics 모델 19%, 신규
    torchvision 모델 17.9% 오탐. docs/flood_water_dataset_workflow.md 3-B절).

    ⚠️ ROI와의 충돌: ROI 좌표는 ``scripts/roi_editor.py``가 **원본(크롭 전)**
    프레임에서 찍은 것이라, 크롭하면 좌표가 어긋난다. 따라서 ``auto`` 모드에서는
    도로 ROI가 설정된 경우 크롭하지 않는다.
    """
    if mode == "off":
        return None
    frame = get_preview_frame(source)
    if frame is None:
        return None
    h, w = frame.shape[:2]
    t, b, l, r = detect_letterbox(frame)
    if (t, b, l, r) == (0, h, 0, w):
        return None                                  # 여백 없음

    if mode == "auto" and pipeline.roi.has_road:
        print(f"[flood] 경고: 레터박스 감지(좌{l} 우{w - r} 상{t} 하{h - b}px)됐으나 "
              "도로 ROI가 설정돼 있어 크롭하지 않습니다 "
              "(ROI 좌표가 원본 기준이라 어긋남). 검은 여백이 물로 오탐될 수 있습니다.")
        print("[flood]   해결: 크롭된 프레임으로 ROI를 다시 지정하거나 "
              "--crop-letterbox on 으로 강제 크롭하세요.")
        return None

    print(f"[flood] 레터박스 크롭 적용: 좌{l} 우{w - r} 상{t} 하{h - b}px")
    return t, b, l, r


def process_run(
    pipeline: Pipeline,
    source: dict[str, Any],
    save: bool = False,
    writer_factory=None,
    process_every_seconds: float | None = None,
    crop_letterbox: str = "auto",
) -> Iterator[tuple[FrameResult, Any]]:
    """Generator: process every sampled frame, yield (FrameResult, writer).

    ``writer_factory``, if given, is called once as
    ``writer_factory(source_name=..., config_used=..., video_fps=...)`` and
    must return an object exposing ``.add(metrics, annotated_bgr)`` and
    ``.finalize()``. This is how Phase 6's
    ``common.case_archive.run_writer.RunWriter`` plugs in without this module
    needing to import the archive package (dependency inversion — Phase 2 has
    no archive dependency).

    ``process_every_seconds``, if given, overrides ``pipeline.mcfg``'s
    ``process_every_seconds`` (e.g. so the CLI's ``--every`` flag actually
    takes effect instead of being silently ignored in favor of the model
    config default).

    ``crop_letterbox``: ``auto``(기본) | ``on`` | ``off``. 검은 여백을 물로
    오인하는 문제를 막기 위한 전처리 — :func:`_resolve_letterbox_crop` 참고.
    """
    pipeline.reset_state()
    every = (
        float(process_every_seconds) if process_every_seconds is not None
        else float(pipeline.mcfg.get("process_every_seconds", 1))
    )

    writer = None
    if save and writer_factory is not None:
        config_used = {
            "water_model_path": pipeline.mcfg.get("water_model_path"),
            "object_model_path": pipeline.mcfg.get("object_model_path"),
            "water_conf": pipeline.mcfg.get("water_conf"),
            "object_conf": pipeline.mcfg.get("object_conf"),
            "iou": pipeline.mcfg.get("iou"),
            "imgsz": pipeline.mcfg.get("imgsz"),
            "process_every_seconds": every,
            "use_tracking": pipeline.use_tracking,
            "device": str(pipeline.bundle.device),
            "roi_camera": pipeline.roi.camera_name,
        }
        writer = writer_factory(
            source_name=source_name(source),
            config_used=config_used,
            video_fps=max(1.0, 1.0 / every if every else 1.0),
        )

    box = _resolve_letterbox_crop(pipeline, source, crop_letterbox)
    for frame_number, ts, frame in iter_source(source, every):
        if box is not None:
            t, b, l, r = box
            frame = frame[t:b, l:r]
        result = pipeline.process_frame(frame, frame_number, ts)
        if writer is not None:
            writer.add(result.metrics, result.annotated_bgr(pipeline.roi))
        yield result, writer


def run_to_completion(pipeline: Pipeline, source: dict[str, Any], writer_factory=None) -> dict[str, Any]:
    """Run a full source headlessly and return the finalized run summary."""
    writer = None
    last: FrameResult | None = None
    for result, writer in process_run(pipeline, source, save=True, writer_factory=writer_factory):
        last = result
    summary = writer.finalize() if writer is not None else {}
    if last is not None:
        summary["last_alert_level"] = last.metrics.alert_level
    return summary


def main() -> None:
    """CLI entry point (``tot-flood-standalone``): headless single-video run.

    By default the run is archived via ``common.case_archive.run_writer.RunWriter``
    (annotated frames + video, each with the ROI/water/detection overlay drawn
    in -- see ``FrameResult.annotated_bgr`` above) so it shows up in the
    ``tot-service`` dashboard's "분석 결과" (Analysis Results) tab. Pass
    ``--no-archive`` to skip that and only print per-frame numbers.
    """
    import argparse

    from ..common.case_archive.run_writer import RunWriter

    ap = argparse.ArgumentParser(description="Standalone single-camera flood pipeline")
    ap.add_argument("--video", required=True, help="path to a video file")
    ap.add_argument("--every", type=float, default=1.0, help="seconds between processed frames")
    ap.add_argument("--roi", default=None,
                    help="path to a common.roi RoiConfig JSON file (e.g. configs/roi/UNDERPATH-01.json). "
                         "Without this, water_area_ratio is measured over the WHOLE frame, not just "
                         "the road, which is rarely what you want for a real camera.")
    ap.add_argument("--no-archive", action="store_true",
                    help="don't save annotated frames/video/CSV under data/runs/ -- "
                         "just print per-frame numbers")
    ap.add_argument("--crop-letterbox", choices=["auto", "on", "off"], default="auto",
                    help="검은 여백(레터박스) 자동 제거. 여백을 그대로 두면 모델이 물로 "
                         "오인한다. auto=여백이 있고 도로 ROI가 없을 때만 크롭(ROI 좌표는 "
                         "원본 기준이라 크롭 시 어긋남), on=항상, off=안 함")
    args = ap.parse_args()

    roi = load_roi_config(args.roi) if args.roi else RoiConfig()
    pipeline = Pipeline.from_config(roi=roi)
    source = {"type": "video", "path": args.video}

    save = not args.no_archive
    writer = None
    for result, writer in process_run(pipeline, source, save=save,
                                      writer_factory=RunWriter if save else None,
                                      process_every_seconds=args.every,
                                      crop_letterbox=args.crop_letterbox):
        print(
            f"frame={result.frame_number} t={result.timestamp_sec:.1f}s "
            f"water_ratio={result.metrics.water_area_ratio:.4f} "
            f"alert={result.metrics.alert_level} risk={result.metrics.risk_score}"
        )

    if writer is not None:
        summary = writer.finalize()
        print(f"\n[archived] run_id={summary['run_id']} frames={summary['frames']} "
              f"max_alert_level={summary['max_alert_level']}")
        print(f"  saved to: {summary['run_dir']}")
        print(f"  view in the dashboard (start it with `tot-service`) under "
              f"the '분석 결과' tab, or at "
              f"/api/flood-runs/{summary['run_id']}")


if __name__ == "__main__":
    main()
