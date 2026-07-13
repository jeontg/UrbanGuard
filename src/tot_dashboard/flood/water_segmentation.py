"""Water-surface segmentation using the custom YOLO11-seg model (``models/best.pt``).

Canonical version adopted from flood3's ``flood/water_segmentation.py``, which
is underpath_flood_dashboard's original module plus a corrupted-frame
detector added for live HLS streams. Confirmed identical core logic between
the two originals during the integration review; the only behavioral
difference was the default confidence (0.25 in underpath_flood_dashboard's
function signature vs. 0.10 in flood3's — both projects actually run at 0.10
via their config files, per underpath_flood_dashboard's README calibration
notes, so 0.10 is adopted as the function default here too).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


def is_likely_corrupted_frame(frame: np.ndarray, tile: int = 16,
                              flat_std_thresh: float = 4.0,
                              flat_area_frac_thresh: float = 0.20) -> bool:
    """Heuristic for H.264 decode corruption on live HLS streams.

    Splits the frame into ``tile``x``tile`` blocks, computes each block's
    brightness std-dev, and flags the frame as corrupted if too large a
    fraction of blocks are near-flat (error-concealment artifact). Segmenting
    a corrupted frame as-is can misclassify decode-corruption noise as water.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    h2, w2 = (h // tile) * tile, (w // tile) * tile
    if h2 < tile or w2 < tile:
        return False
    gray = gray[:h2, :w2]
    tiles = gray.reshape(h2 // tile, tile, w2 // tile, tile).swapaxes(1, 2)
    stds = tiles.std(axis=(2, 3))
    flat_fraction = float((stds < flat_std_thresh).mean())
    return flat_fraction > flat_area_frac_thresh


@dataclass
class WaterResult:
    mask: np.ndarray            # uint8 (H, W), 0 or 255 = flood_water
    num_instances: int          # number of water polygons detected
    water_pixels: int           # total water pixels in the full frame
    max_confidence: float       # highest instance confidence (0 if none)
    raw: Any = None             # underlying ultralytics Results (for overlays)


def _masks_to_binary(result, height: int, width: int) -> tuple[np.ndarray, int]:
    """Build a full-res 0/255 mask from a YOLO-seg Results object.

    Uses ``masks.xy`` (polygons already in original-image coordinates) which
    avoids letterbox/resize ambiguity.
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    masks = getattr(result, "masks", None)
    if masks is None or masks.xy is None or len(masks.xy) == 0:
        return mask, 0
    count = 0
    for poly in masks.xy:
        if poly is None or len(poly) < 3:
            continue
        cv2.fillPoly(mask, [np.asarray(poly, dtype=np.int32)], 255)
        count += 1
    return mask, count


def segment_water(
    model,
    frame: np.ndarray,
    conf: float = 0.10,
    iou: float = 0.50,
    imgsz: int = 640,
    device: Any = "cpu",
) -> WaterResult:
    """Run water segmentation on a single BGR frame."""
    h, w = frame.shape[:2]
    results = model.predict(
        frame, conf=conf, iou=iou, imgsz=imgsz, device=device, verbose=False
    )
    result = results[0]
    mask, n = _masks_to_binary(result, h, w)

    max_conf = 0.0
    if getattr(result, "masks", None) is not None and result.boxes is not None:
        try:
            confs = result.boxes.conf
            if confs is not None and len(confs) > 0:
                max_conf = float(confs.max())
        except Exception:
            max_conf = 0.0

    return WaterResult(
        mask=mask,
        num_instances=n,
        water_pixels=int(np.count_nonzero(mask)),
        max_confidence=max_conf,
        raw=result,
    )
