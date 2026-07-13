"""Save and reload time-series processing runs (flood-domain archive style).

Ported from underpath_flood_dashboard's ``src/archive_manager.py``.

★ Kept as a SEPARATE archival mechanism from ``catalog.py`` (SAM's static
case-manifest reader), not merged into it (docs/integration_plan.md section
3-5): ``RunWriter`` writes a *time series* (one CSV row per processed frame)
built incrementally while a pipeline runs, whereas ``CaseCatalog`` reads a
*pre-built static manifest* describing already-finished artifact files. These
are genuinely different data models serving different producers (a live
frame-by-frame pipeline vs. a batch/Colab job that already finished) — see
the integration review's explicit correction of the original plan, which had
proposed unifying them.

Run layout::

    data/runs/run_YYYYMMDD_HHMMSS/
        config_used.json
        metrics.csv
        alert_log.csv
        annotated_frames/
        snapshots/
        processed_video.mp4
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT, load_json, save_json
from ..video_io import open_h264_video_writer
from ...flood.metrics_core import FloodMetrics

RUNS_DIR = PROJECT_ROOT / "data" / "runs"

METRICS_COLUMNS = [
    "run_id", "source_name", "frame_number", "timestamp_sec",
    "water_area_pixels", "road_roi_area_pixels", "water_area_ratio",
    "water_expansion_rate", "person_count", "vehicle_count",
    "avg_vehicle_pixel_speed", "traffic_state", "alert_level", "alert_reason",
    "risk_score", "risk_grade", "risk_trend", "pred_water_ratio_10s",
    "pred_risk_10s", "eta_danger_sec", "vehicles_tire_in_water",
    "max_vehicle_submersion",
]

ALERT_COLUMNS = [
    "run_id", "timestamp_sec", "frame_number", "alert_level", "alert_reason",
    "water_area_ratio", "person_count", "vehicle_count", "traffic_state",
    "snapshot_path",
]


def new_run_id(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now()
    return now.strftime("run_%Y%m%d_%H%M%S")


class RunWriter:
    """Accumulates per-frame outputs and writes a complete run on finalize."""

    def __init__(
        self,
        source_name: str,
        config_used: dict[str, Any],
        runs_dir: Path = RUNS_DIR,
        save_annotated_frames: bool = True,
        build_video: bool = True,
        video_fps: float = 1.0,
        snapshot_min_level: int = 3,
    ) -> None:
        self.run_id = new_run_id()
        self.source_name = source_name
        self.run_dir = Path(runs_dir) / self.run_id
        self.frames_dir = self.run_dir / "annotated_frames"
        self.snaps_dir = self.run_dir / "snapshots"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(exist_ok=True)
        self.snaps_dir.mkdir(exist_ok=True)

        self.save_annotated_frames = save_annotated_frames
        self.build_video = build_video
        self.video_fps = max(1.0, float(video_fps))
        self.snapshot_min_level = snapshot_min_level

        cfg = dict(config_used)
        cfg["run_id"] = self.run_id
        cfg["source_name"] = source_name
        save_json(self.run_dir / "config_used.json", cfg)

        self._metric_rows: list[dict[str, Any]] = []
        self._alert_rows: list[dict[str, Any]] = []
        self._writer: cv2.VideoWriter | None = None
        self._prev_level = 1
        self.video_path = self.run_dir / "processed_video.mp4"

    def add(self, m: FloodMetrics, annotated_bgr: np.ndarray | None) -> None:
        traffic_state = m.traffic_state.value if hasattr(m.traffic_state, "value") else m.traffic_state
        row = {
            "run_id": self.run_id,
            "source_name": self.source_name,
            "frame_number": m.frame_number,
            "timestamp_sec": round(m.timestamp_sec, 3),
            "water_area_pixels": m.water_area_pixels,
            "road_roi_area_pixels": m.road_roi_area_pixels,
            "water_area_ratio": round(m.water_area_ratio, 5),
            "water_expansion_rate": round(m.water_expansion_rate, 6),
            "person_count": m.person_count,
            "vehicle_count": m.vehicle_count,
            "avg_vehicle_pixel_speed": (
                "" if m.avg_vehicle_pixel_speed is None
                else round(m.avg_vehicle_pixel_speed, 2)
            ),
            "traffic_state": traffic_state,
            "alert_level": m.alert_level,
            "alert_reason": m.alert_reason,
            "risk_score": m.risk_score,
            "risk_grade": m.risk_grade,
            "risk_trend": m.risk_trend,
            "pred_water_ratio_10s": round(m.pred_water_ratio_10s, 5),
            "pred_risk_10s": round(m.pred_risk_10s, 1),
            "eta_danger_sec": m.eta_danger_sec,
            "vehicles_tire_in_water": m.vehicles_tire_in_water,
            "max_vehicle_submersion": round(m.max_vehicle_submersion, 3),
        }
        self._metric_rows.append(row)

        snapshot_path = ""
        if annotated_bgr is not None:
            if self.save_annotated_frames:
                fp = self.frames_dir / f"frame_{m.frame_number:06d}.jpg"
                cv2.imwrite(str(fp), annotated_bgr)
            if m.alert_level >= self.snapshot_min_level or m.alert_level > self._prev_level:
                sp = self.snaps_dir / f"alert{m.alert_level}_{m.frame_number:06d}.jpg"
                cv2.imwrite(str(sp), annotated_bgr)
                snapshot_path = str(sp.relative_to(self.run_dir))
            if self.build_video:
                self._write_video_frame(annotated_bgr)

        if m.alert_level >= 3 or m.alert_level != self._prev_level:
            self._alert_rows.append({
                "run_id": self.run_id,
                "timestamp_sec": round(m.timestamp_sec, 3),
                "frame_number": m.frame_number,
                "alert_level": m.alert_level,
                "alert_reason": m.alert_reason,
                "water_area_ratio": round(m.water_area_ratio, 5),
                "person_count": m.person_count,
                "vehicle_count": m.vehicle_count,
                "traffic_state": traffic_state,
                "snapshot_path": snapshot_path,
            })
        self._prev_level = m.alert_level

    def _write_video_frame(self, frame_bgr: np.ndarray) -> None:
        if self._writer is None:
            h, w = frame_bgr.shape[:2]
            self._writer = open_h264_video_writer(self.video_path, self.video_fps, (w, h))
        self._writer.write(frame_bgr)

    def finalize(self) -> dict[str, Any]:
        metrics_df = pd.DataFrame(self._metric_rows, columns=METRICS_COLUMNS)
        metrics_df.to_csv(self.run_dir / "metrics.csv", index=False)
        alert_df = pd.DataFrame(self._alert_rows, columns=ALERT_COLUMNS)
        alert_df.to_csv(self.run_dir / "alert_log.csv", index=False)
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "frames": len(self._metric_rows),
            "events": len(self._alert_rows),
            "max_alert_level": int(metrics_df["alert_level"].max()) if len(metrics_df) else 1,
            "video": str(self.video_path) if self.video_path.exists() else "",
        }


@dataclass
class RunSummary:
    run_id: str
    run_dir: Path
    frames: int
    max_alert_level: int
    source_name: str
    has_video: bool


def list_runs(runs_dir: Path = RUNS_DIR) -> list[RunSummary]:
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []
    out: list[RunSummary] = []
    for d in sorted(runs_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        metrics_csv = d / "metrics.csv"
        if not metrics_csv.exists():
            continue  # incomplete/interrupted run -> hide from archive
        frames = 0
        max_level = 1
        source = ""
        try:
            df = pd.read_csv(metrics_csv)
            frames = len(df)
            max_level = int(df["alert_level"].max()) if frames else 1
            source = str(df["source_name"].iloc[0]) if frames else ""
        except Exception:  # noqa: BLE001
            pass
        out.append(RunSummary(
            run_id=d.name, run_dir=d, frames=frames, max_alert_level=max_level,
            source_name=source, has_video=(d / "processed_video.mp4").exists(),
        ))
    return out


def load_run(run_dir: str | Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    metrics = pd.read_csv(run_dir / "metrics.csv") if (run_dir / "metrics.csv").exists() else pd.DataFrame()
    alerts = pd.read_csv(run_dir / "alert_log.csv") if (run_dir / "alert_log.csv").exists() else pd.DataFrame()
    config = load_json(run_dir / "config_used.json") if (run_dir / "config_used.json").exists() else {}
    snaps = sorted((run_dir / "snapshots").glob("*.jpg")) if (run_dir / "snapshots").exists() else []
    frames = sorted((run_dir / "annotated_frames").glob("*.jpg")) if (run_dir / "annotated_frames").exists() else []
    video = run_dir / "processed_video.mp4"
    return {
        "run_dir": run_dir,
        "metrics": metrics,
        "alerts": alerts,
        "config": config,
        "snapshots": snaps,
        "annotated_frames": frames,
        "video": video if video.exists() else None,
    }
