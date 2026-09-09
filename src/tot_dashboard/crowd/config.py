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

# ═══════════════════════════════════════════════════════════════════════
#  ★ 인파 밀집도 — 대조한 외부 기준은 전부 「명/㎡」입니다
# ═══════════════════════════════════════════════════════════════════════
#  국내
#    · 1㎡당 **3명 주의 / 4명 경계 / 5명 심각** — 지자체 인파관리 체계에서
#      통용되는 구분. ⚠️ 행정안전부 공식 지침 원문은 확인하지 못했습니다.
#      인용 전 「다중운집인파 안전관리」 지침 원문 확인이 필요합니다.
#    · 이태원 참사 분석: 1㎡당 약 11명, 약 0.5톤의 압력 (2023-01 보도)
#  국외
#    · **Fruin Level of Service (LOS A~F)** — 보행 밀도·유동률로 A(자유)~
#      F(정체)를 나누는 표준 척도. 군중 안전 분야의 사실상 국제 기준
#    · 영국 Green Guide(Guide to Safety at Sports Grounds): 입석 관람 구역
#      **4.7명/㎡**
#    · 영국 이벤트 실무: 일반 안전 한계 **2명/㎡**, 이동 대기열 **4명/㎡**
#    · 위험 기울기: 1~2명/㎡ 자유 이동 → **4~5명/㎡ 위험 급증** →
#      6~7명/㎡ 자발적 움직임 상실. **5명/㎡ 이상이 압사 임계**로 통용
#
#  ⚠️ **우리는 명/㎡ 를 재지 못합니다.**
#     ``ALERT_DENSITY`` 는 화면을 격자로 나눠 **사람이 점유한 칸의 비율(%)**
#     입니다. 실제 바닥 면적(㎡)이 아니라 **화면 픽셀 기준**이라, 위 기준을
#     그대로 옮길 수 없습니다. 같은 40% 라도 원거리 카메라와 근거리 카메라의
#     실제 밀도는 전혀 다릅니다.
#
#  ⇒ 남은 과제: **지면 평면 캘리브레이션(호모그래피)**. 화면 좌표를 실제
#     바닥 좌표로 변환해야 「명/㎡」를 낼 수 있고, 그래야 위 기준과 연결됩니다.
#     캘리브레이션이 되면 아래 목표 대응으로 재설정하십시오.
#
#  [목표 대응] 캘리브레이션 후 각 단계가 뜻해야 하는 밀도
#     주의   ≒ 3명/㎡ (국내)      · 경계 ≒ 4명/㎡ (국내 · 영국 대기열 한계)
#     심각   ≒ 5명/㎡ (국내 · 국제 압사 임계)
# ═══════════════════════════════════════════════════════════════════════
# [환산불가·자체] 격자 점유율(%) 기준값. 명/㎡ 가 아닙니다.
ALERT_DENSITY = 40.0

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
