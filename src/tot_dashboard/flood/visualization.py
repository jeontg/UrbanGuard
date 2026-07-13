"""Drawing helpers for the flood-domain single-camera views.

Ported from underpath_flood_dashboard's ``src/visualization.py``. Adapted for
the canonical multi-polygon ``common.roi.RoiConfig`` (road_roi/low_point_roi
are now a *list* of polygon pieces rather than one flat point list) — the
original ``draw_roi`` only ever drew ``roi.road_roi[0]`` as if it were the
whole polygon; here every piece is drawn. Also updated to call
``common.roi.point_in_polygons`` instead of the retired single-polygon
``point_in_polygon`` (see docs/integration_plan.md section 2).

All low-level functions work on BGR images (OpenCV convention). The
high-level ``render_*`` helpers return RGB images ready for display.
"""
from __future__ import annotations

import cv2
import numpy as np

from ..common.roi import RoiConfig, point_in_polygons
from .object_detection import DetectionResult
from .water_segmentation import WaterResult

# BGR colors
WATER_COLOR = (255, 64, 0)        # blue (water shown in blue)
ROAD_ROI_COLOR = (0, 220, 0)      # green
LOW_POINT_COLOR = (255, 0, 200)   # magenta
LANE_LINE_COLOR = (0, 220, 220)   # cyan
PERSON_COLOR = (0, 140, 255)      # orange
VEHICLE_COLOR = (255, 180, 0)     # azure
DANGER_COLOR = (0, 0, 255)        # red


def bgr_to_rgb(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


# --------------------------------------------------------------------------
def draw_roi(image: np.ndarray, roi: RoiConfig, show_road=True, show_low=True,
             show_line=True) -> np.ndarray:
    """Draw every ROI polygon piece + lane line onto a copy of the BGR image."""
    out = image.copy()
    if show_road and roi.has_road:
        for i, poly in enumerate(roi.road_roi):
            if len(poly) < 3:
                continue
            pts = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(out, [pts], True, ROAD_ROI_COLOR, 2)
            if i == 0:
                _label(out, "road ROI", poly[0], ROAD_ROI_COLOR)
    if show_low and roi.has_low_point:
        for i, poly in enumerate(roi.low_point_roi):
            if len(poly) < 3:
                continue
            pts = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(out, [pts], True, LOW_POINT_COLOR, 2)
            if i == 0:
                _label(out, "low point", poly[0], LOW_POINT_COLOR)
    if show_line and roi.has_lane_line:
        p1 = tuple(int(v) for v in roi.lane_threshold_line[0])
        p2 = tuple(int(v) for v in roi.lane_threshold_line[1])
        cv2.line(out, p1, p2, LANE_LINE_COLOR, 2)
        _label(out, "lane line", roi.lane_threshold_line[0], LANE_LINE_COLOR)
    return out


def overlay_water(frame: np.ndarray, water: WaterResult, roi: RoiConfig | None = None,
                  alpha: float = 0.45, show_roi: bool = True) -> np.ndarray:
    """Blue translucent water mask + bright contour, optional ROI overlay."""
    out = frame.copy()
    mask = water.mask
    if mask is not None and np.any(mask):
        color_layer = np.zeros_like(out)
        color_layer[mask > 0] = WATER_COLOR
        out = cv2.addWeighted(out, 1.0, color_layer, alpha, 0)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (255, 255, 255), 2)
    if show_roi and roi is not None:
        out = draw_roi(out, roi, show_low=True, show_line=True)
    return out


def overlay_detections(frame: np.ndarray, detection: DetectionResult,
                       roi: RoiConfig | None = None, show_roi: bool = False,
                       show_track_id: bool = True) -> np.ndarray:
    """Bounding boxes for persons/vehicles with labels."""
    out = frame.copy()
    if show_roi and roi is not None:
        out = draw_roi(out, roi, show_low=False, show_line=False)
    for d in detection.detections:
        x1, y1, x2, y2 = (int(v) for v in d.box)
        color = PERSON_COLOR if d.category == "person" else VEHICLE_COLOR
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = d.class_name
        if show_track_id and d.track_id is not None:
            label += f" #{d.track_id}"
        label += f" {d.confidence:.2f}"
        _label(out, label, (x1, y1), color)
    return out


def _label(img: np.ndarray, text: str, anchor, color) -> None:
    x, y = int(anchor[0]), int(anchor[1])
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), base = cv2.getTextSize(text, font, 0.5, 1)
    y_top = max(0, y - th - base - 2)
    cv2.rectangle(img, (x, y_top), (x + tw + 4, y_top + th + base + 2), color, -1)
    cv2.putText(img, text, (x + 2, y_top + th + 1), font, 0.5, (0, 0, 0), 1, cv2.LINE_AA)


# --------------------------------------------------------------------------
# High-level: return RGB images
# --------------------------------------------------------------------------
def render_raw(frame: np.ndarray, roi: RoiConfig | None = None, show_roi=False) -> np.ndarray:
    out = draw_roi(frame, roi) if (show_roi and roi is not None) else frame
    return bgr_to_rgb(out)


def render_water(frame: np.ndarray, water: WaterResult, roi: RoiConfig | None = None,
                 show_roi=True) -> np.ndarray:
    return bgr_to_rgb(overlay_water(frame, water, roi, show_roi=show_roi))


def render_detection(frame: np.ndarray, detection: DetectionResult,
                     roi: RoiConfig | None = None, show_roi=False) -> np.ndarray:
    return bgr_to_rgb(overlay_detections(frame, detection, roi, show_roi=show_roi))


# --------------------------------------------------------------------------
# Combined annotation (used for the saved processed_video) + alert banner
# --------------------------------------------------------------------------
def draw_alert_banner(image: np.ndarray, level: int, reason: str = "") -> np.ndarray:
    """Draw a colored status bar across the top of a BGR image."""
    from .alert_engine import AlertEngine  # local import to avoid cycle

    out = image.copy()
    name, hex_ = AlertEngine.describe_en(level)   # ASCII for cv2.putText
    bgr = _hex_to_bgr(hex_)
    h, w = out.shape[:2]
    bar_h = max(28, h // 18)
    cv2.rectangle(out, (0, 0), (w, bar_h), bgr, -1)
    text = f"ALERT {level} - {name}"
    if reason and reason.isascii():   # Korean reasons live in the CSV/UI, not the cv2 banner
        text += f"  |  {reason}"
    text = text[:120]
    cv2.putText(out, text, (10, int(bar_h * 0.7)), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def annotate_combined(frame: np.ndarray, water: WaterResult, detection: DetectionResult,
                      roi: RoiConfig | None, level: int = 1, reason: str = "") -> np.ndarray:
    """One BGR frame with water + detections + ROI + alert banner (for video)."""
    out = overlay_water(frame, water, roi, show_roi=True)
    out = overlay_detections(out, detection, roi, show_roi=False)
    out = draw_alert_banner(out, level, reason)
    return out


def _hex_to_bgr(hex_: str) -> tuple[int, int, int]:
    hex_ = hex_.lstrip("#")
    r, g, b = int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)
    return (b, g, r)


# --------------------------------------------------------------------------
# Risk overlay: highlight submerged vehicles / endangered persons
# --------------------------------------------------------------------------
def _tire_zone_wet(box, mask: np.ndarray) -> bool:
    x1, y1, x2, y2 = (int(round(c)) for c in box)
    h, w = mask.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return False
    band = max(3, int(0.20 * (y2 - y1)))
    region = mask[y2 - band:y2, x1:x2]
    return bool(region.size and np.any(region > 0))


def _point_wet(point, mask: np.ndarray) -> bool:
    x, y = int(round(point[0])), int(round(point[1]))
    h, w = mask.shape[:2]
    return 0 <= x < w and 0 <= y < h and mask[y, x] > 0


def draw_risk_overlay(frame: np.ndarray, water: WaterResult,
                      detection: DetectionResult, roi: RoiConfig | None,
                      risk=None, prediction=None) -> np.ndarray:
    """Water + ROI, with submerged vehicles / endangered persons boxed in red
    and a risk banner (score, grade, trend, ETA) across the top."""
    out = overlay_water(frame, water, roi, show_roi=True)
    mask = water.mask
    for d in detection.detections:
        x1, y1, x2, y2 = (int(v) for v in d.box)
        danger = False
        if mask is not None and np.any(mask):
            if d.category == "vehicle":
                danger = _tire_zone_wet(d.box, mask)
            else:
                danger = _point_wet(d.bottom_center, mask) or (
                    roi is not None and roi.has_low_point
                    and point_in_polygons(d.bottom_center, roi.low_point_roi))
        color = DANGER_COLOR if danger else (
            PERSON_COLOR if d.category == "person" else VEHICLE_COLOR)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 3 if danger else 2)
        tag = ("PERSON" if d.category == "person" else "VEHICLE")
        if danger:
            tag += " WET"
        _label(out, tag, (x1, y1), color)
    if risk is not None:
        out = _draw_risk_banner(out, risk, prediction)
    return out


def _draw_risk_banner(image: np.ndarray, risk, prediction=None) -> np.ndarray:
    from .alert_engine import LEVEL_COLOR   # reuse the grade palette
    out = image
    _, hex_ = LEVEL_COLOR.get(risk.risk_grade, ("Gray", "#616161"))
    bgr = _hex_to_bgr(hex_)
    h, w = out.shape[:2]
    bar_h = max(30, h // 16)
    cv2.rectangle(out, (0, 0), (w, bar_h), bgr, -1)
    text = f"RISK {risk.risk_score:.0f}/100  (G{risk.risk_grade})"
    if prediction is not None:
        trend = {"surge": "UP+", "rising": "UP", "stable": "=",
                 "falling": "DOWN", "insufficient": "?"}.get(prediction.trend_label, "?")
        text += f"  trend {trend}"
        eta_d = prediction.eta.get("danger") if prediction.eta else None
        if eta_d is not None and eta_d > 0:
            text += f"  ETA danger {eta_d:.0f}s"
    cv2.putText(out, text[:120], (10, int(bar_h * 0.7)), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def render_risk(frame: np.ndarray, water: WaterResult, detection: DetectionResult,
                roi: RoiConfig | None = None, risk=None, prediction=None) -> np.ndarray:
    return bgr_to_rgb(draw_risk_overlay(frame, water, detection, roi, risk, prediction))
