"""Flood-domain config loaders with baked-in defaults.

Ported from underpath_flood_dashboard's ``src/config_utils.py``
(``load_model_config``/``load_alert_config``/``load_risk_config``), rebuilt on
top of ``common.config.load_config_simple`` /
``common.config.load_config_with_nested_merge`` (Phase 1) instead of
duplicating the YAML-merge logic. Paths point at this repo's top-level
``configs/`` directory (flood3's convention — plural, shared by all domains)
rather than underpath_flood_dashboard's original singular ``config/``.
"""
from __future__ import annotations

from typing import Any

from ..common.config import PROJECT_ROOT, load_config_simple, load_config_with_nested_merge

CONFIG_DIR = PROJECT_ROOT / "configs"
MODEL_CONFIG_PATH = CONFIG_DIR / "flood_model_config.yaml"
ALERT_CONFIG_PATH = CONFIG_DIR / "alert_config.yaml"
RISK_CONFIG_PATH = CONFIG_DIR / "risk_config.yaml"


def load_model_config() -> dict[str, Any]:
    """Model/inference config with safe defaults if the file is missing."""
    defaults: dict[str, Any] = {
        "water_model_path": "models/best.pt",
        "object_model_path": "models/yolo11s.pt",
        "water_conf": 0.10,
        "object_conf": 0.30,
        "iou": 0.50,
        "imgsz": 640,
        "device": "auto",
        "process_every_seconds": 1,
        "person_classes": ["person"],
        "vehicle_classes": ["car", "bus", "truck", "motorcycle"],
        "use_tracking": True,
        "tracker": "bytetrack.yaml",
    }
    return load_config_simple(MODEL_CONFIG_PATH, defaults)


def load_alert_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "ratio_watch": 0.02,
        "ratio_caution": 0.08,
        "ratio_danger": 0.20,
        "ratio_shutdown": 0.35,
        "expansion_rate_rising": 0.01,
        "persistence_frames": 3,
        "stopped_speed_px": 2.0,
        "slow_speed_px": 8.0,
        "jam_min_vehicles": 3,
        "cooldown_frames": 3,
    }
    return load_config_simple(ALERT_CONFIG_PATH, defaults)


def load_risk_config() -> dict[str, Any]:
    """Risk-scoring weights + prediction settings."""
    defaults: dict[str, Any] = {
        "weights": {
            "area": 0.28,
            "low_point": 0.24,
            "lane": 0.10,
            "expansion": 0.10,
            "tire": 0.12,
            "person": 0.10,
            "traffic": 0.06,
        },
        "expansion_ref": 0.05,
        "person_danger_floor": 85.0,
        "shutdown_rising_floor": 90.0,
        "grade_bins": [20, 40, 60, 80],
        "horizons_sec": [5, 10],
        "predict_window_sec": 12.0,
        "predict_min_points": 3,
        "trend_surge": 0.03,
        "trend_rising": 0.005,
        "trend_falling": -0.005,
        "eta_max_sec": 600,
    }
    return load_config_with_nested_merge(RISK_CONFIG_PATH, defaults, nested_keys=("weights",))
