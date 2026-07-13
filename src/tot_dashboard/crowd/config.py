"""Crowd (SAM3) domain configuration and checkpoint path resolution.

Ported from SAM's ``SAM3_install.py`` CONFIG section. This whole domain needs
a local NVIDIA GPU (CUDA 12.6+, PyTorch 2.7+) and a ~3.2GB ``sam3.pt``
checkpoint that is NOT tracked in git (docs/integration_plan.md section 5-7,
8) — none of it can be exercised in a CPU-only environment. It is installed
via the optional ``crowd-gpu`` extra (``pip install -e ".[crowd-gpu]"``).

★ Bug fix (docs/integration_plan.md sections 2/8): the integration review
found that SAM's ``sam3.pt`` was physically sitting at the SAM project root,
while ``SAM3_install.py``'s default ``SAM3_ROOT`` expected it at
``SAM3_ROOT/sam3.pt`` — a real, confirmed path mismatch, not a hypothetical
one. :func:`resolve_checkpoint` now checks both the configured
``SAM3_ROOT`` and the repo root before failing, with a clear error naming
both locations, instead of a bare ``assert``.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..common.config import PROJECT_ROOT

SAM3_ROOT = Path(os.environ.get("SAM3_ROOT", str(PROJECT_ROOT / "SAM3")))
SAM3_REPO = SAM3_ROOT / "sam3_src"
OUTPUT_DIR = SAM3_ROOT / "results"

PROMPT = "person"          # detection text prompt
SCORE_TH = 0.4             # confidence threshold
FRAME_STRIDE = 15          # analyze every N frames
ALERT_DENSITY = 40.0       # density (%) alert threshold

# Grid density heatmap (ratio-based -> auto-scales with resolution)
GRID_COLS = 96
INFLUENCE_RADIUS_RATIO = 0.035
HEATMAP_ALPHA = 0.55
USE_FEET_POINT = True

# VLM (control-center warning text)
GEMINI_MODEL = "gemini-2.5-flash"
KEY_NAMES = ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
LOCATION_NAME = "야간 상업지구-A"
CCTV_ID = "CAM-01"

RUNTIME_DEPS = [
    "iopath>=0.1.10", "timm>=1.0.17", "tqdm",
    "ftfy==6.1.1", "regex", "typing_extensions", "huggingface_hub",
    "einops", "opencv-python", "scikit-image", "scikit-learn",
    "matplotlib", "pillow", "scipy", "supervision", "google-genai",
]


def resolve_checkpoint() -> Path:
    """Locate ``sam3.pt``, checking both the confirmed-buggy default location
    and the repo root (see module docstring)."""
    candidates = [SAM3_ROOT / "sam3.pt", PROJECT_ROOT / "sam3.pt"]
    for c in candidates:
        if c.exists():
            return c
    checked = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"sam3.pt checkpoint not found. Checked: {checked}. "
        "Set the SAM3_ROOT env var or place sam3.pt in one of these locations."
    )


def ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR
