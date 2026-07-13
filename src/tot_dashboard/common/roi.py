"""Region-of-interest (ROI) helpers — canonical multi-polygon version.

Ported from flood3's ``flood/roi_utils.py``, which is itself an extension of
underpath_flood_dashboard's ``src/roi_utils.py``. flood3's version is adopted
as the canonical implementation because it is a strict superset: a block
(intersection) can have its road/low-point ROI split into several polygon
pieces (``PolygonList``), and legacy single-polygon files (the format
underpath_flood_dashboard's ``config/roi_config.json`` uses) are transparently
upgraded to a one-item polygon list on load via :func:`_normalize_polygons`.

Confirmed during the integration review (docs/integration_plan.md section 2)
that this is a genuine API change from the single-polygon version, not just an
additive one: ``point_in_polygon(point, polygon)`` became
``point_in_polygons(point, polygons)``. Any code ported from
underpath_flood_dashboard that called ``point_in_polygon`` must be updated to
call ``point_in_polygons`` instead (see flood/standalone_pipeline.py in Phase 2).

Saved JSON shape::

    {
      "camera_name": "BLOCK-CHORYANG",
      "frame_width": 1280,
      "frame_height": 720,
      "road_roi": [ [[x,y], [x,y], ...], [[x,y], ...], ... ],   # polygon pieces
      "low_point_roi": [ [[x,y], ...], ... ],                   # polygon pieces (optional)
      "lane_threshold_line": [[x1,y1],[x2,y2]]                  # optional single segment
    }

Old single-polygon format ``[[x,y], ...]`` is auto-upgraded to a one-item list.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

Polygon = list[list[int]]
PolygonList = list[Polygon]


def _normalize_polygons(value: Any) -> PolygonList:
    """Normalize road_roi/low_point_roi raw value -> list of polygons.

    New format: [[[x,y],...], [[x,y],...]]  (several polygon pieces)
    Old format: [[x,y], [x,y], ...]         (one polygon, point list) -> [old_format]
    """
    if not value:
        return []
    first = value[0]
    if first and isinstance(first[0], (list, tuple)):
        return [list(poly) for poly in value]  # already new format
    return [list(value)]  # old format -> one-item polygon list


@dataclass
class RoiConfig:
    camera_name: str = ""
    frame_width: int | None = None
    frame_height: int | None = None
    road_roi: PolygonList | None = None
    low_point_roi: PolygonList | None = None
    lane_threshold_line: list[list[int]] | None = None

    def __post_init__(self) -> None:
        self.road_roi = _normalize_polygons(self.road_roi or [])
        self.low_point_roi = _normalize_polygons(self.low_point_roi or [])
        self.lane_threshold_line = self.lane_threshold_line or []

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoiConfig":
        return cls(
            camera_name=d.get("camera_name", ""),
            frame_width=d.get("frame_width"),
            frame_height=d.get("frame_height"),
            road_roi=d.get("road_roi") or [],
            low_point_roi=d.get("low_point_roi") or [],
            lane_threshold_line=d.get("lane_threshold_line") or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_name": self.camera_name,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "road_roi": self.road_roi,
            "low_point_roi": self.low_point_roi,
            "lane_threshold_line": self.lane_threshold_line,
        }

    @property
    def has_road(self) -> bool:
        return any(len(p) >= 3 for p in (self.road_roi or []))

    @property
    def has_low_point(self) -> bool:
        return any(len(p) >= 3 for p in (self.low_point_roi or []))

    @property
    def has_lane_line(self) -> bool:
        return len(self.lane_threshold_line or []) == 2


def load_roi_config(path: str | Path) -> RoiConfig:
    path = Path(path)
    if not path.exists():
        return RoiConfig()
    with open(path, encoding="utf-8") as fh:
        return RoiConfig.from_dict(json.load(fh))


def save_roi_config(cfg: RoiConfig, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Rasterization / geometry — all operate on a PolygonList (list of pieces)
# --------------------------------------------------------------------------
def _to_int_array(points: Polygon | np.ndarray) -> np.ndarray:
    return np.asarray(points, dtype=np.int32).reshape(-1, 2)


def polygon_mask(polygons: PolygonList, height: int, width: int) -> np.ndarray:
    """Filled uint8 mask (0/255), all polygon pieces combined.

    Pieces with fewer than 3 points are ignored. Empty list -> all zeros.
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    valid = [_to_int_array(p) for p in (polygons or []) if p and len(p) >= 3]
    if valid:
        cv2.fillPoly(mask, valid, 255)
    return mask


def polygon_area_pixels(polygons: PolygonList, height: int, width: int) -> int:
    if not polygons:
        return 0
    return int(np.count_nonzero(polygon_mask(polygons, height, width)))


def point_in_polygons(point: tuple[float, float], polygons: PolygonList) -> bool:
    """True if the point falls inside any one of the polygon pieces."""
    if not polygons:
        return False
    p = (float(point[0]), float(point[1]))
    for poly in polygons:
        if poly is None or len(poly) < 3:
            continue
        if cv2.pointPolygonTest(_to_int_array(poly), p, False) >= 0:
            return True
    return False


def water_crosses_line(water_mask: np.ndarray, line: list[list[int]], band: int = 6) -> bool:
    """Does the water mask touch the lane threshold line (rasterized as a thin band)?"""
    if line is None or len(line) != 2:
        return False
    h, w = water_mask.shape[:2]
    band_mask = np.zeros((h, w), dtype=np.uint8)
    p1 = tuple(int(v) for v in line[0])
    p2 = tuple(int(v) for v in line[1])
    cv2.line(band_mask, p1, p2, color=255, thickness=max(1, band))
    overlap = cv2.bitwise_and(water_mask, band_mask)
    return bool(np.any(overlap))
