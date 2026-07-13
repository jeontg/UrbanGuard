"""Background pipeline runner — updates per-block 5-stage inference + flood
risk (measurement/prediction) at a live pace.

Ported from flood3's ``service/runner.py``, with import paths updated for the
merged package layout (docs/integration_plan.md Phase 7) and two behavioral
changes carried over from earlier phases:
  - the duplicated ``_load_yaml`` helper is now ``common.config``'s shared
    loader (Phase 1);
  - notification goes through the promoted ``common.notifier.AlertNotifier``
    instead of the retired per-block ``DryRunNotifier`` (Phase 4) — one
    shared notifier instance is used for all blocks (env-configured
    recipients aren't per-block in the SOLAPI settings model), with the
    ``run_poc.py``-style message-building helper reused via
    :func:`_notify_decision`.

**1 block = 1 camera (source) + per-block rainfall + adjacent river**.
Declared per block in blocks.json:
  source  : {"type":"synthetic"|"hls"|"rtsp"|"video", ...}
  rainfall: {"type":"sine"(default)|"mock"|"kma"}
  river   : {"type":"none"(default)|"mock"|"hrfco", advisory_m/warning_m/danger_m, ...}
A failed video/stream/external-API only degrades that one signal (the
dashboard keeps running). VLM is only called at snapshot time (~1s) to save
cost (falls back to rules if no key).

★ Flood risk (① water area ② expansion ③ RiskEngine ④ RiskPredictor):
  for real video sources (hls/rtsp/video) water segmentation
  (``models/best.pt``) runs on the received frame and is scored via
  ``flood.flood_metrics_engine``/``flood.risk_engine``. The existing vehicle
  detection/ByteTrack/rainfall pipeline is unchanged and its output
  (VehicleObject, TrafficMetrics, person points) is reused for traffic/person
  signals (no duplicate detection). Runs at a lower cadence (default 1s) than
  vehicle tracking (5fps) since water changes slowly.
"""
from __future__ import annotations

import threading
from datetime import datetime

from ..common.config import load_config_with_nested_merge, PROJECT_ROOT
from ..common.models_loader import resolve_device
from ..common.notifier import AlertNotifier, DuplicateNotificationError, NotificationConfigurationError
from ..common.roi import load_roi_config
from ..flood.flood_metrics_engine import FloodMetricsEngine
from ..flood.risk_engine import RiskEngine, RiskPredictor, write_back
from ..flood.water_segmentation import WaterResult, is_likely_corrupted_frame, segment_water
from ..traffic_weather.agents.risk_decision import RiskDecisionAgent
from ..traffic_weather.agents.semantic_agent import SemanticAgent
from ..traffic_weather.agents.vlm_situation import VlmSituationAgent
from ..traffic_weather.perception.detection_source import SyntheticDetectionSource
from ..traffic_weather.perception.perception import Perception
from ..traffic_weather.perception.rainfall_provider import SineRainfallProvider, build_rainfall
from ..traffic_weather.perception.render import frame_to_image, render_scene, to_jpeg_b64
from ..traffic_weather.perception.river_provider import build_river

_RISK_DEFAULTS = {
    "weights": {"area": 0.28, "low_point": 0.24, "lane": 0.10, "expansion": 0.10,
                "tire": 0.12, "person": 0.10, "traffic": 0.06},
    "expansion_ref": 0.05, "person_danger_floor": 85.0, "shutdown_rising_floor": 90.0,
    "grade_bins": [20, 40, 60, 80],
    "ratio_watch": 0.02, "ratio_caution": 0.08, "ratio_danger": 0.20, "ratio_shutdown": 0.35,
    "horizons_sec": [5, 10], "predict_window_sec": 12.0, "predict_min_points": 3,
    "trend_surge": 0.03, "trend_rising": 0.005, "trend_falling": -0.005, "eta_max_sec": 600,
}
_WATER_DEFAULTS = {
    "water_model_path": "models/best.pt", "water_conf": 0.10,
    "iou": 0.50, "imgsz": 640, "device": "auto", "process_every_seconds": 1.0,
}


def _notify_decision(notifier: AlertNotifier, decision, block_name: str) -> None:
    """Build a generic alert message from a Decision and send it (dry-run by
    default; see ``traffic_weather.run_poc._notify``, the same pattern)."""
    body = f"[{decision.level.value}] {block_name} — {decision.risk_name}: {decision.recommendation}"
    message = {
        "body": body, "severity_label": decision.level.value, "location": block_name,
        "context": decision.context, "recommendation": decision.recommendation,
        "video_time": f"{decision.t_sec:.1f}s", "risk_score": decision.score,
    }
    try:
        notifier.send(event_key=f"{block_name}:{decision.risk_code}", message=message,
                      channels=["sms"])
    except (DuplicateNotificationError, NotificationConfigurationError):
        pass  # cooldown or not configured -- same as the original silent gate


def _snapshot(block, weather, metrics, situation, decision, river, snap_b64, vlm_src, kind,
              vehicles=None, flood=None) -> dict:
    objs = sorted(vehicles or [], key=lambda v: -v.speed_drop)[:6]
    mpp = (block.get("calib") or {}).get("mpp", 0.06)  # meters-per-pixel (per-camera calibration, rough default)
    snap = {
        "mpp": mpp, "mean_speed_kmh": round(metrics.mean_speed * mpp * 3.6, 1),
        "stream_url": (block.get("source") or {}).get("url"),
        "stream_type": (block.get("source") or {}).get("type"),
        "block_id": block["id"], "name": block["name"],
        "coordinates": block["coordinates"], "source_kind": kind,
        "t_sec": metrics.t_sec, "rain_mm_h": weather.rain_mm_h,
        "intensity": weather.intensity.value, "rain_source": weather.source,
        "rain_trend": weather.trend, "rain_delta": weather.delta_mm_h,
        "mean_speed": metrics.mean_speed, "speed_drop": metrics.speed_drop,
        "state": metrics.state.value, "queue_len": metrics.queue_len,
        "stalled": metrics.stalled, "n_vehicles": metrics.n_vehicles,
        "objects": [{"id": v.track_id, "speed": v.speed,
                     "kmh": round(v.speed * mpp * 3.6, 1), "drop": v.speed_drop,
                     "stalled": v.stalled} for v in objs],
        "river_level": round(river.level_m, 2) if river else None,
        "river_status": river.status.value if river else None,
        "river_source": river.source if river else None,
        "risk_code": decision.risk_code, "risk_name": decision.risk_name,
        "level": decision.level.value, "severity": decision.severity,
        "score": decision.score, "recommendation": decision.recommendation,
        "situation_ko": situation.get("situation_ko", ""),
        "vlm_source": vlm_src, "drivers": decision.drivers, "alert": decision.alert,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "snapshot": snap_b64,
    }
    if flood is not None:
        m, risk, pred = flood
        snap.update({
            "water_available": True,
            "water_area_ratio": round(m.water_area_ratio, 4),
            "water_expansion_rate": round(m.water_expansion_rate, 5),
            "water_roi_defined": m.roi_defined,
            "water_near_low_point": m.water_near_low_point,
            "water_crosses_lane": m.water_crosses_lane,
            "vehicles_tire_in_water": m.vehicles_tire_in_water,
            "persons_in_danger": m.persons_in_danger,
            "stopped_vehicles_near_water": m.stopped_vehicles_near_water,
            "flood_risk_score": risk.risk_score,
            "flood_risk_grade": risk.risk_grade,
            "flood_risk_grade_label": risk.grade_label,
            "flood_risk_top_reason": risk.top_reason,
            "flood_risk_components": risk.components,
            "flood_pred_trend": pred.trend_label,
            "flood_pred_confidence": pred.confidence,
            "flood_pred_ratio_10s": round(pred.pred_ratio.get(10, m.water_area_ratio), 4),
            "flood_pred_risk_10s": pred.pred_risk.get(10, risk.risk_score),
            "flood_eta_danger_sec": pred.eta.get("danger"),
            "flood_eta_shutdown_sec": pred.eta.get("shutdown"),
        })
    else:
        snap["water_available"] = False
    return snap


class PipelineRunner(threading.Thread):
    def __init__(self, store, blocks, fps: float = 5.0, use_vlm: bool = False):
        super().__init__(daemon=True)
        self.store = store
        self.fps = fps
        self.dt = 1.0 / fps
        self.use_vlm = use_vlm
        self._stop = threading.Event()
        self._ctx: dict[str, dict] = {}
        self._workers: list = []
        self._notifier = AlertNotifier()

        self.risk_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "risk_config.yaml", _RISK_DEFAULTS)
        self.water_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "water_config.yaml", _WATER_DEFAULTS)
        self._water_device = resolve_device(self.water_cfg.get("device", "auto"))
        self._water_model_path = PROJECT_ROOT / self.water_cfg["water_model_path"]

        for b in blocks:
            rp = b.get("rain", {})
            sine = SineRainfallProvider(peak_mm_h=rp.get("peak", 25.0),
                                        period_sec=rp.get("period", 48.0),
                                        phase_sec=rp.get("phase", 0.0))
            rainfall = build_rainfall(b.get("rainfall"), b["coordinates"], fallback=sine)
            river = build_river(b.get("river"))
            source, baseline_hint, kind = self._build_source(b, rainfall)
            roi = load_roi_config(PROJECT_ROOT / "configs" / "roi" / f"{b['id']}.json")
            self._ctx[b["id"]] = {
                "block": b, "rainfall": rainfall, "river": river,
                "frames": source.frames(), "wh": source.frame_wh, "kind": kind,
                "perception": Perception(b, rainfall, fps=self.fps,
                                         baseline_hint=baseline_hint, use_bytetrack=True),
                "vlm": VlmSituationAgent(use_vlm=use_vlm, location=b["name"]),
                "semantic": SemanticAgent(), "decider": RiskDecisionAgent(),
                "last_snap_t": -999.0, "last_b64": None,
                "water_model": self._load_water_model() if kind in ("hls", "rtsp", "video") else None,
                "roi": roi,
                "flood_engine": FloodMetricsEngine(roi=roi),
                "risk_engine": RiskEngine(self.risk_cfg, self.risk_cfg),
                "risk_predictor": None,  # filled in below once risk_engine exists
                "last_water_t": -999.0, "last_flood": None,
                "frame_no": 0,
            }
            self._ctx[b["id"]]["risk_predictor"] = RiskPredictor(
                self._ctx[b["id"]]["risk_engine"], self.risk_cfg)

    def _load_water_model(self):
        try:
            from ultralytics import YOLO
            return YOLO(str(self._water_model_path))
        except Exception as e:  # noqa: BLE001
            print(f"[runner] water segmentation model load failed ({self._water_model_path}): "
                  f"{str(e)[:120]}")
            return None

    def _build_source(self, block, rainfall):
        """Block source config -> (source, baseline_hint, kind). Falls back to synthetic."""
        cfg = block.get("source") or {"type": "synthetic"}
        stype = cfg.get("type", "synthetic")
        if stype in ("video", "rtsp", "hls"):
            target = cfg.get("url") or cfg.get("path")
            try:
                from ..traffic_weather.perception.detection_source import YoloDetectionSource
                src = YoloDetectionSource(target, fps=self.fps, loop=True)
                print(f"[runner] {block['id']}: connected YOLO {stype} source '{target}'")
                return src, None, stype
            except Exception as e:  # noqa: BLE001
                print(f"[runner] {block['id']}: {stype} '{target}' connection failed -> synthetic "
                      f"fallback ({str(e)[:90]})")
        src = SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None)
        return src, src.base_speed, "synthetic"

    def _next_frame(self, c):
        try:
            return next(c["frames"])
        except StopIteration:  # video ended (empty/short clip) -> synthetic fallback
            src = SyntheticDetectionSource(c["rainfall"], fps=self.fps, duration_sec=None)
            c["frames"] = src.frames()
            c["wh"] = src.frame_wh
            c["kind"] = "synthetic(fallback)"
            c["perception"] = Perception(c["block"], c["rainfall"], fps=self.fps,
                                         baseline_hint=src.base_speed, use_bytetrack=True)
            c["water_model"] = None  # no real frames in synthetic fallback -> no water seg
            return next(c["frames"])

    def run(self) -> None:
        # one independent worker thread per camera (block) -> each stream is
        # tracked continuously so ByteTrack ids persist and speed isn't 0
        # (a single round-robin thread is too slow for 4+ streams to
        # accumulate 2 track points, reading 0px/s)
        for bid, c in self._ctx.items():
            th = threading.Thread(target=self._block_loop, args=(bid, c), daemon=True)
            th.start()
            self._workers.append(th)
        self._stop.wait()

    def _update_flood(self, c, t: float, frame, ps) -> None:
        """Water segmentation -> flood metrics (①②) -> risk (③) -> prediction (④).

        Skipped (last computed value kept) if there's no water model (load
        failed) or no real frame (synthetic source) -- the dashboard keeps
        running either way."""
        if c["water_model"] is None or frame is None:
            return
        every = float(self.water_cfg.get("process_every_seconds", 1.0))
        if t - c["last_water_t"] < every:
            return
        c["last_water_t"] = t
        # H.264 decode-corruption guard -- segmenting a corrupted frame as-is
        # can misclassify decode noise as water (observed in practice). Skip
        # this tick (don't advance the counter) and keep the last good value.
        if is_likely_corrupted_frame(frame):
            c["corrupted_skips"] = c.get("corrupted_skips", 0) + 1
            if c["corrupted_skips"] in (1, 10) or c["corrupted_skips"] % 50 == 0:
                print(f"[runner:flood] treated as corrupted frame, skipping "
                      f"(total {c['corrupted_skips']})")
            return
        c["frame_no"] += 1
        try:
            water: WaterResult = segment_water(
                c["water_model"], frame,
                conf=self.water_cfg.get("water_conf", 0.10),
                iou=self.water_cfg.get("iou", 0.50),
                imgsz=self.water_cfg.get("imgsz", 640),
                device=self._water_device,
            )
            flood_m = c["flood_engine"].update(
                water, ps.vehicles, ps.persons, ps.metrics, c["frame_no"], t)
            risk = c["risk_engine"].score(flood_m)
            flood_m.risk_score, flood_m.risk_grade = risk.risk_score, risk.risk_grade
            pred = c["risk_predictor"].update(flood_m)
            write_back(flood_m, risk, pred)
            c["last_flood"] = (flood_m, risk, pred)
        except Exception as e:  # noqa: BLE001
            print(f"[runner:flood] {str(e)[:120]}")

    def _block_loop(self, bid, c) -> None:
        while not self._stop.is_set():
            try:
                t, dets, frame = self._next_frame(c)
                ps = c["perception"].step(t, dets, c["wh"])  # ① perception: update object attributes
                weather, metrics = ps.weather, ps.metrics
                river = c["river"].at(t)
                self._update_flood(c, t, frame, ps)  # flood risk (measurement/prediction)
                img = None
                if t - c["last_snap_t"] >= 1.0:  # snapshot roughly every 1s
                    img = (frame_to_image(frame, dets) if frame is not None
                           else render_scene(dets, weather, c["wh"]))
                    c["last_b64"] = to_jpeg_b64(img)
                    c["last_snap_t"] = t
                situation = c["vlm"].interpret(img, metrics, weather,
                                               location=c["block"]["name"])
                risk = c["semantic"].infer(weather, metrics, situation,
                                           context={"block": c["block"]["name"]}, river=river)
                decision = c["decider"].decide(risk, t)
                if decision.alert:
                    _notify_decision(self._notifier, decision, c["block"]["name"])
                self.store.update(bid, _snapshot(
                    c["block"], weather, metrics, situation, decision, river,
                    c["last_b64"], c["vlm"].last_source, c["kind"], ps.vehicles,
                    flood=c["last_flood"]))
            except Exception as e:  # noqa: BLE001
                print(f"[runner:{bid}] {str(e)[:120]}")
            self._stop.wait(self.dt)

    def stop(self) -> None:
        self._stop.set()
