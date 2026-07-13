"""Grid-based density heatmap utilities (ratio-based, distance-weighted).

Ported from SAM's ``SAM3_install.py`` (Stage 7). Generic "points/boxes ->
density heatmap overlay" utility, decoupled from SAM3 itself — operates on
plain box arrays regardless of what produced them (SAM3, YOLO, ...), so it is
fully testable on CPU. Flagged during the integration review
(docs/integration_plan.md section 8) as a good candidate for reuse by any
future "points -> heatmap" need.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter

from .config import GRID_COLS, HEATMAP_ALPHA, INFLUENCE_RADIUS_RATIO, USE_FEET_POINT


def make_grid(W: int, H: int, cols: int | None = None) -> tuple[int, int, float]:
    cols = int(cols or GRID_COLS)
    cell = W / cols
    rows = max(1, round(H / cell))
    return cols, rows, cell


def grid_density(boxes, W: int, H: int, cols: int | None = None,
                  influence_ratio: float | None = None) -> tuple[np.ndarray, tuple[int, int, float]]:
    cols, rows, cell = make_grid(W, H, cols)
    influence_ratio = INFLUENCE_RADIUS_RATIO if influence_ratio is None else influence_ratio
    grid = np.zeros((rows, cols), np.float32)
    if len(boxes):
        b = np.asarray(boxes, np.float32)
        px = (b[:, 0] + b[:, 2]) * 0.5
        py = b[:, 3] if USE_FEET_POINT else (b[:, 1] + b[:, 3]) * 0.5
        gx = np.clip((px / cell).astype(int), 0, cols - 1)
        gy = np.clip((py / cell).astype(int), 0, rows - 1)
        np.add.at(grid, (gy, gx), 1.0)
    sigma_cells = max(influence_ratio * W / cell, 0.5)
    grid = gaussian_filter(grid, sigma=sigma_cells, mode="constant")
    return grid, (cols, rows, cell)


def density_to_heatmap(grid: np.ndarray, W: int, H: int,
                        normalize_max: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    g = grid.copy()
    mx = normalize_max if normalize_max else (g.max() if g.max() > 0 else 1.0)
    g = np.clip(g / mx, 0, 1)
    g_full = cv2.resize((g * 255).astype(np.uint8), (W, H), interpolation=cv2.INTER_CUBIC)
    heat = cv2.applyColorMap(g_full, cv2.COLORMAP_JET)
    return heat, g_full


def overlay_heatmap(bgr_frame: np.ndarray, heat: np.ndarray, alpha: float | None = None,
                     mask_low: bool = True) -> np.ndarray:
    alpha = HEATMAP_ALPHA if alpha is None else alpha
    out = cv2.addWeighted(bgr_frame, 1 - alpha, heat, alpha, 0)
    if mask_low:
        gray = cv2.cvtColor(heat, cv2.COLOR_BGR2GRAY)
        out[gray < 30] = bgr_frame[gray < 30]
    return out
