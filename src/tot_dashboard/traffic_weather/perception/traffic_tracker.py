"""TrafficBehaviorTracker — tracks vehicle detections into per-frame TrafficMetrics.

Ported from flood3's ``perception/traffic_tracker.py``, itself a retargeting
of SAM's ``CrowdBehaviorTracker`` (crowd speed-surge -> vehicle speed-drop /
queueing / stalled vehicles). This SAM-to-flood3 lineage is a second, distinct
code-reuse path beyond the water-segmentation/risk-engine port from
underpath_flood_dashboard — see docs/integration_plan.md section 2.

Tracking backend priority:
1. Detections already carry a track_id -> use as-is (synthetic = ideal
   tracking, or an external tracker).
2. ``use_bytetrack=True`` -> ``supervision.ByteTrack`` (falls back to centroid
   if not installed).
3. Default -> built-in centroid matching (numpy, no dependency).

Speed is averaged over several track-history frames; speed_drop is
EMA-smoothed to damp per-frame flicker in the derived grade.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

import numpy as np

from ...models import Detection, TrafficMetrics, TrafficState, VehicleObject


class _CentroidTracker:
    """Lightweight nearest-centroid matching tracker (no dependencies)."""

    def __init__(self, max_dist: float = 90.0, max_lost: int = 5):
        self.max_dist = max_dist
        self.max_lost = max_lost
        self.next_id = 1
        self.objects: dict[int, tuple[float, float]] = {}
        self.lost: dict[int, int] = {}

    def update(self, centroids: list[tuple[float, float]]) -> list[int]:
        ids: list[int] = [-1] * len(centroids)
        if not self.objects:
            for i, c in enumerate(centroids):
                ids[i] = self._register(c)
            return ids
        obj_ids = list(self.objects.keys())
        if centroids:
            obj_pts = np.array([self.objects[i] for i in obj_ids], dtype=float)
            cur = np.array(centroids, dtype=float)
            dist = np.linalg.norm(obj_pts[:, None, :] - cur[None, :, :], axis=2)
            pairs = sorted((dist[m, n], m, n)
                           for m in range(len(obj_ids)) for n in range(len(centroids)))
            used_obj: set[int] = set()
            used_cur: set[int] = set()
            for d, m, n in pairs:
                if d > self.max_dist:
                    break
                oid = obj_ids[m]
                if oid in used_obj or n in used_cur:
                    continue
                self.objects[oid] = (cur[n][0], cur[n][1])
                self.lost[oid] = 0
                ids[n] = oid
                used_obj.add(oid)
                used_cur.add(n)
            for n, c in enumerate(centroids):
                if ids[n] == -1:
                    ids[n] = self._register(c)
            for oid in obj_ids:
                if oid not in used_obj:
                    self._mark_lost(oid)
        else:
            for oid in obj_ids:
                self._mark_lost(oid)
        return ids

    def _register(self, c: tuple[float, float]) -> int:
        i = self.next_id
        self.next_id += 1
        self.objects[i] = (c[0], c[1])
        self.lost[i] = 0
        return i

    def _mark_lost(self, oid: int) -> None:
        self.lost[oid] = self.lost.get(oid, 0) + 1
        if self.lost[oid] > self.max_lost:
            self.objects.pop(oid, None)
            self.lost.pop(oid, None)


def _center(bbox) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


class TrafficBehaviorTracker:
    def __init__(self, fps: float = 5.0, hist: int = 8,
                 stall_speed: float = 15.0, slow_speed: float = 60.0,
                 speed_window: int = 4, ema_alpha: float = 0.5,
                 baseline_hint: float | None = None,
                 use_bytetrack: bool = False):
        self.fps = fps
        self.hist = hist
        self.stall_speed = stall_speed  # px/s below this = stalled vehicle
        self.slow_speed = slow_speed  # px/s below this = queued
        self.speed_window = speed_window  # frames averaged for speed
        self.ema_alpha = ema_alpha
        self.tracks: dict[int, deque] = defaultdict(lambda: deque(maxlen=hist))
        self._age: dict[int, int] = {}  # per-track frames-tracked age
        self.last_objects: list[VehicleObject] = []  # most recent frame's vehicle objects
        self._speed_hist: deque = deque(maxlen=int(max(fps, 1) * 6))
        # free-flow baseline (running max of rolling median). A hint gives an
        # immediate accurate drop ratio (synthetic = known base_speed); real
        # YOLO sources have none -> learned from observation.
        self._baseline = float(baseline_hint) if baseline_hint else 0.0
        self._n_seen = 0
        self._drop_ema: float | None = None
        self._frame_area: float | None = None
        self.backend, self._bt = self._init_backend(use_bytetrack)
        self._centroid = _CentroidTracker()

    def _init_backend(self, use_bytetrack: bool):
        if use_bytetrack:
            try:
                import supervision as sv
                return "bytetrack", sv.ByteTrack(frame_rate=int(max(self.fps, 1)))
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] supervision unavailable -> centroid fallback: {str(e)[:80]}")
        return "centroid", None

    def _track(self, detections: list[Detection], t: float):
        """Returns list[(bbox, track_id)] — backend-agnostic."""
        if detections and all(d.track_id is not None for d in detections):
            return [(d.bbox, int(d.track_id)) for d in detections]
        if self.backend == "bytetrack":
            import supervision as sv
            if not detections:
                self._bt.update_with_detections(sv.Detections.empty())
                return []
            det = sv.Detections(
                xyxy=np.array([d.bbox for d in detections], dtype=float),
                confidence=np.array([d.conf for d in detections], dtype=float),
                class_id=np.zeros(len(detections), dtype=int))
            det = self._bt.update_with_detections(det)
            return [(tuple(xyxy), int(tid)) for xyxy, tid in zip(det.xyxy, det.tracker_id)]
        centroids = [_center(d.bbox) for d in detections]
        ids = self._centroid.update(centroids)
        return [(d.bbox, tid) for d, tid in zip(detections, ids)]

    def update(self, detections: list[Detection], t: float,
               frame_wh: tuple[int, int] | None = None) -> TrafficMetrics:
        if frame_wh:
            self._frame_area = float(frame_wh[0] * frame_wh[1])
        tracked = self._track(detections, t)

        per = []  # (bbox, tid, speed, has_speed)
        speeds: list[float] = []
        for bbox, tid in tracked:
            cx, cy = _center(bbox)
            self.tracks[tid].append((t, cx, cy))
            self._age[tid] = self._age.get(tid, 0) + 1
            h = self.tracks[tid]
            sp_obj, has = 0.0, len(h) >= 2
            if has:  # average speed over the recent speed_window frames
                k = min(self.speed_window, len(h))
                (t0, x0, y0), (t1, x1, y1) = h[-k], h[-1]
                sp_obj = math.hypot(x1 - x0, y1 - y0) / max(t1 - t0, 1e-3)
                speeds.append(sp_obj)
            per.append((bbox, tid, sp_obj, has))

        sp = np.array(speeds, dtype=float) if speeds else np.zeros(0)
        mean_speed = float(sp.mean()) if sp.size else 0.0

        # free-flow baseline: running max of rolling median (early no-rain window is the reference).
        if sp.size:
            self._speed_hist.append(mean_speed)
            self._n_seen += 1
            if self._n_seen >= 3:
                self._baseline = max(self._baseline, float(np.median(self._speed_hist)))
        base = self._baseline if self._baseline > 1e-6 else mean_speed
        raw_drop = min(max(1.0 - mean_speed / base, 0.0), 1.0) if base > 1e-6 else 0.0
        self._drop_ema = raw_drop if self._drop_ema is None \
            else self.ema_alpha * raw_drop + (1 - self.ema_alpha) * self._drop_ema
        speed_drop = self._drop_ema

        stalled = int((sp < self.stall_speed).sum()) if sp.size else 0
        queue_len = int((sp < self.slow_speed).sum()) if sp.size else 0

        density = 0.0
        if self._frame_area:
            area = sum(max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
                       for b, _ in tracked)
            density = min(area / self._frame_area, 1.0)

        # ── per-vehicle object attributes: speed + drop vs. free flow ──
        objs = []
        for bbox, tid, sp_obj, has in per:
            drop = min(max(1.0 - sp_obj / base, 0.0), 1.0) if (base > 1e-6 and has) else 0.0
            objs.append(VehicleObject(
                track_id=int(tid), bbox=tuple(float(v) for v in bbox),
                speed=round(sp_obj, 1), speed_drop=round(drop, 3),
                age=self._age[tid], stalled=bool(has and sp_obj < self.stall_speed)))
        self.last_objects = objs

        return TrafficMetrics(
            t_sec=round(t, 2), n_vehicles=len(tracked),
            mean_speed=round(mean_speed, 2), speed_drop=round(speed_drop, 3),
            density=round(density, 3), queue_len=queue_len, stalled=stalled,
            state=self._state(speed_drop, stalled, mean_speed))

    def _state(self, drop: float, stalled: int, mean_speed: float) -> TrafficState:
        if mean_speed < self.stall_speed and stalled >= 2:
            return TrafficState.blocked
        if drop > 0.5:
            return TrafficState.congested
        if drop > 0.2:
            return TrafficState.slow
        return TrafficState.free
