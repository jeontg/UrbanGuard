"""Vehicle/person detection sources (CCTV AI stage).

Ported from flood3's ``perception/detection_source.py``. **1 camera (video/
stream) = 1 block (intersection)**; each block owns its own source. Two
implementations behind the ``DetectionSource`` protocol:
- ``SyntheticDetectionSource`` — deterministic synthetic scene, no model/video/
  GPU needed. Rain slows/queues vehicles (PoC validation). Stable track_id per
  vehicle.
- ``YoloDetectionSource`` — real ultralytics YOLO on recorded video/RTSP.
  Dependency is lazy-imported. Tracking is left to
  ``TrafficBehaviorTracker`` (ByteTrack/centroid).

★ Integration-review fix (docs/integration_plan.md section 3-2): the original
flood3 file hard-coded COCO class ids (``_COCO_VEHICLE_IDS``/
``_COCO_PERSON_IDS``), which the review flagged as violating the "resolve
classes by name, never hard-code ids" rule that underpath_flood_dashboard's
``model_loader.py`` follows. This port resolves target class ids by name via
``common.models_loader.select_class_ids`` instead.

``frames()`` yields ``(t, detections, frame)``. ``frame`` is the real video's
BGR frame (for snapshots/VLM); synthetic sources yield ``None`` (the caller
renders the synthetic scene instead).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Protocol

from ...common.models_loader import select_class_ids
from ...models import Detection

VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}
PERSON_CLASSES = {"person"}

Frame = tuple[float, list[Detection], object | None]  # (t_sec, detections, frame|None)


class DetectionSource(Protocol):
    fps: float
    frame_wh: tuple[int, int]

    def frames(self) -> Iterator[Frame]: ...


# ──────────────────────────────────────────────────────────────────────────
# Synthetic source — rain up -> slowdown/queueing. Stable per-vehicle id.
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class _Veh:
    id: int
    lane: int
    x: float
    factor: float  # per-vehicle speed deviation (0.85~1.15)
    length: float = 70.0
    width: float = 36.0


class SyntheticDetectionSource:
    def __init__(self, rainfall, fps: float = 5.0, duration_sec: float | None = 24.0,
                 frame_w: int = 1280, frame_h: int = 720, lanes: int = 3,
                 base_speed_px_s: float = 180.0, n_per_lane: int = 6):
        self.rainfall = rainfall
        self.fps = fps
        self.duration = duration_sec
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.lanes = lanes
        self.base_speed = base_speed_px_s
        self.dt = 1.0 / fps
        self._spacing = frame_w / n_per_lane
        self._lane_y = [int(frame_h * (i + 1) / (lanes + 1)) for i in range(lanes)]
        self._next_id = 1
        self._veh: list[_Veh] = []
        for ln in range(lanes):
            for k in range(n_per_lane):
                self._veh.append(self._spawn(ln, k * self._spacing))

    @property
    def frame_wh(self) -> tuple[int, int]:
        return self.frame_w, self.frame_h

    def _spawn(self, lane: int, x: float) -> _Veh:
        vid = self._next_id
        self._next_id += 1
        factor = 0.85 + 0.30 * ((vid * 37) % 100) / 100.0  # deterministic deviation
        return _Veh(id=vid, lane=lane, x=x, factor=factor)

    def _rain_factor(self, t: float) -> float:
        """Rain -> desired-speed multiplier (0.06~1.0). Strong slowdown near 25mm/h."""
        rain = self.rainfall.at(t).rain_mm_h
        return max(0.06, 1.0 - (rain / 30.0) * 0.95)

    def frames(self) -> Iterator[Frame]:
        margin = 120.0
        t = 0.0
        eps = self.dt * 0.5
        while self.duration is None or t <= self.duration + eps:  # None = continuous demo
            desired = self.base_speed * self._rain_factor(t)  # px/s
            for v in self._veh:
                v.x += desired * v.factor * self.dt
            # exits right -> re-enters at left tail with a new id (keeps flow, no track confusion)
            for v in self._veh:
                if v.x > self.frame_w + margin:
                    lane_min = min(o.x for o in self._veh if o.lane == v.lane and o is not v)
                    v.x = lane_min - self._spacing
                    v.id = self._next_id
                    self._next_id += 1
                    v.factor = 0.85 + 0.30 * ((v.id * 37) % 100) / 100.0
            dets: list[Detection] = []
            for v in self._veh:
                if -v.length <= v.x <= self.frame_w:  # only detect what's on-screen
                    y = self._lane_y[v.lane]
                    dets.append(Detection(
                        bbox=(v.x, y - v.width / 2, v.x + v.length, y + v.width / 2),
                        conf=1.0, cls="car", track_id=v.id))
            yield round(t, 3), dets, None
            t += self.dt


# ──────────────────────────────────────────────────────────────────────────
# Real video/RTSP source — YOLO (ultralytics). Lazy import. loop=True repeats.
# ──────────────────────────────────────────────────────────────────────────
class YoloDetectionSource:
    def __init__(self, video_path: str, model: str = "models/yolo11s.pt",
                 fps: float = 5.0, conf: float = 0.3, loop: bool = False,
                 imgsz: int = 416):
        self.video_path = video_path
        self.model_name = model
        self.fps = fps
        self.conf = conf
        self.loop = loop
        self.imgsz = imgsz  # inference resolution (lower = faster) -> real-time across streams
        try:
            import cv2  # noqa: F401
            from ultralytics import YOLO
        except ImportError as e:
            raise RuntimeError(
                "Real-video (YOLO) path needs ultralytics/opencv-python. "
                "`pip install ultralytics opencv-python` and retry."
            ) from e
        import cv2
        self._cv2 = cv2
        self._model = YOLO(model)
        # Resolve target class ids BY NAME (not hard-coded COCO indices) —
        # see module docstring.
        wanted_names = sorted(VEHICLE_CLASSES | PERSON_CLASSES)
        target_ids = select_class_ids(self._model, wanted_names)
        names = dict(self._model.names)
        self._target_id_to_name: dict[int, str] = {cid: names[cid] for cid in target_ids}
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Cannot open video/stream: {video_path}")
        self._src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_wh = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280,
                         int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720)
        cap.release()

    def frames(self) -> Iterator[Frame]:
        cv2 = self._cv2
        target_ids = list(self._target_id_to_name)
        while True:
            cap = cv2.VideoCapture(self.video_path)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # live: always the newest frame
            except Exception:  # noqa: BLE001
                pass
            start = time.monotonic()
            produced = False
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                res = self._model(frame, conf=self.conf, imgsz=self.imgsz,
                                  classes=target_ids, verbose=False)[0]
                dets: list[Detection] = []
                for b in res.boxes:
                    cid = int(b.cls[0])
                    if cid not in self._target_id_to_name:
                        continue
                    x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
                    dets.append(Detection(bbox=(x1, y1, x2, y2),
                                          conf=float(b.conf[0]),
                                          cls=self._target_id_to_name[cid]))
                # wall-clock elapsed -> speed computed from real dt, independent of processing speed
                yield round(time.monotonic() - start, 3), dets, frame
                produced = True
            cap.release()
            if not self.loop:  # not looping (one-shot file) -> stop
                break
            if not produced:
                # connected but never got a frame (stream briefly degraded). Reconnect
                # rather than giving up (this used to raise StopIteration and
                # permanently fall back to synthetic).
                time.sleep(1.0)  # avoid reconnect spam
