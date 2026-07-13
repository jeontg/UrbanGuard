"""Person/vehicle detection using ``models/yolo11s.pt``, self-contained
(single YOLO call does both detection AND tracking via ultralytics' built-in
tracker).

Ported verbatim from underpath_flood_dashboard's ``src/object_detection.py``.
Kept separate from flood3's ``perception/detection_source.py`` /
``perception/traffic_tracker.py`` — the integration review found those use an
incompatible data model (Pydantic ``Detection`` with hard-coded COCO class
ids, tracking delegated to a standalone ``TrafficBehaviorTracker``) built for
a different pipeline shape (externally-fed, multi-block server). See
docs/integration_plan.md section 3-2. This module is used only by
``flood/standalone_pipeline.py`` (the self-contained single-camera path).

Target classes are selected by NAME via ``common.models_loader`` — never
hard-coded ids.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Tracking needs `lap` (linear assignment), an optional ultralytics dependency.
# We probe once and fall back to plain detection if it is missing, so the app
# never crashes just because tracking isn't installed.
_TRACK_AVAILABLE: bool | None = None


def _tracking_available() -> bool:
    global _TRACK_AVAILABLE
    if _TRACK_AVAILABLE is None:
        try:
            import lap  # noqa: F401
            _TRACK_AVAILABLE = True
        except Exception:
            _TRACK_AVAILABLE = False
    return _TRACK_AVAILABLE


@dataclass
class Detection:
    class_id: int
    class_name: str
    category: str                 # "person" or "vehicle"
    confidence: float
    box: tuple[float, float, float, float]   # x1, y1, x2, y2
    track_id: int | None = None

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def bottom_center(self) -> tuple[float, float]:
        """Approx. tire/foot contact point - useful for ROI containment."""
        x1, _, x2, y2 = self.box
        return ((x1 + x2) / 2.0, y2)


@dataclass
class DetectionResult:
    detections: list[Detection] = field(default_factory=list)
    raw: Any = None

    @property
    def persons(self) -> list[Detection]:
        return [d for d in self.detections if d.category == "person"]

    @property
    def vehicles(self) -> list[Detection]:
        return [d for d in self.detections if d.category == "vehicle"]


def detect_objects(
    model,
    frame: np.ndarray,
    class_ids: list[int],
    person_class_ids: list[int],
    vehicle_class_ids: list[int],
    conf: float = 0.30,
    iou: float = 0.50,
    imgsz: int = 640,
    device: Any = "cpu",
    track: bool = False,
    tracker: str = "bytetrack.yaml",
) -> DetectionResult:
    """Detect (and optionally track) target objects on a single BGR frame."""
    person_set = set(person_class_ids)
    vehicle_set = set(vehicle_class_ids)
    classes = class_ids if class_ids else None

    if track and _tracking_available():
        results = model.track(
            frame, conf=conf, iou=iou, imgsz=imgsz, classes=classes,
            device=device, persist=True, tracker=tracker, verbose=False,
        )
    else:
        # predict mode (no track ids -> vehicle speed reported as unknown)
        results = model.predict(
            frame, conf=conf, iou=iou, imgsz=imgsz, classes=classes,
            device=device, verbose=False,
        )
    result = results[0]
    names = dict(model.names)

    detections: list[Detection] = []
    boxes = getattr(result, "boxes", None)
    if boxes is not None and len(boxes) > 0:
        xyxy = boxes.xyxy.cpu().numpy()
        cls = boxes.cls.cpu().numpy().astype(int)
        confs = boxes.conf.cpu().numpy()
        ids = (
            boxes.id.cpu().numpy().astype(int)
            if getattr(boxes, "id", None) is not None
            else [None] * len(cls)
        )
        for box, c, cf, tid in zip(xyxy, cls, confs, ids):
            category = "person" if c in person_set else ("vehicle" if c in vehicle_set else "other")
            if category == "other":
                continue
            detections.append(
                Detection(
                    class_id=int(c),
                    class_name=names.get(int(c), str(c)),
                    category=category,
                    confidence=float(cf),
                    box=tuple(float(v) for v in box),
                    track_id=int(tid) if tid is not None else None,
                )
            )

    return DetectionResult(detections=detections, raw=result)
