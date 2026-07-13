"""SAM3 model loading + per-frame dense-segmentation analysis.

Ported from SAM's ``SAM3_install.py`` (env/dependency setup + Stage 1).
**Needs a local NVIDIA GPU (CUDA 12.6+) and PyTorch 2.7+** — none of this
module can be exercised in a CPU-only environment, and it was NOT executable
in this session for that reason (docs/integration_plan.md sections 6/8: run
Phase 5's GPU-dependent verification separately on a CUDA machine). All
GPU/SAM3-source imports are lazy so importing this module on a CPU-only
machine still succeeds (only calling its functions requires the GPU).
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

from .config import SAM3_REPO, resolve_checkpoint, RUNTIME_DEPS

# Offline (block HF network calls once the checkpoint is local)
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_PROCESSOR = None  # set by load_model()


def _pip(args: list[str]) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install"] + args, check=True)


def ensure_deps(force: bool = False) -> None:
    """Install runtime dependencies (only if missing)."""
    print("[*] checking/installing dependencies...")
    if force:
        _pip(["-q"] + RUNTIME_DEPS)
        return
    check = {
        "iopath": "iopath", "timm": "timm", "ftfy": "ftfy", "regex": "regex",
        "einops": "einops", "opencv-python": "cv2", "scikit-image": "skimage",
        "scipy": "scipy", "supervision": "supervision",
        "google-genai": "google.genai", "matplotlib": "matplotlib",
        "huggingface_hub": "huggingface_hub", "pillow": "PIL",
    }
    need = []
    for pip_name, mod in check.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            need.append(pip_name)
    if need:
        print("    installing:", need)
        _pip(["-q"] + need)
    else:
        print("    all dependencies present")
    print("[OK] dependencies ready")


def ensure_source() -> None:
    """Clone the SAM3 source (if missing) and register it on sys.path."""
    if not os.path.exists(os.path.join(SAM3_REPO, "sam3")):
        print("[*] cloning SAM3 source...")
        subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/facebookresearch/sam3.git", str(SAM3_REPO)],
            check=True,
        )
    if str(SAM3_REPO) not in sys.path:
        sys.path.insert(0, str(SAM3_REPO))
    print("[OK] source:", SAM3_REPO)


def check_env() -> bool:
    import torch
    print("Python :", sys.version.split()[0])
    print("PyTorch:", torch.__version__, "| CUDA:", torch.version.cuda,
          "| GPU:", torch.cuda.is_available())
    if not torch.cuda.is_available():
        print("[WARN] no CUDA GPU detected — SAM3 requires CUDA.")
        return False
    print("GPU:", torch.cuda.get_device_name(0),
          "| VRAM:", round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1), "GB")
    mj, mn = (int(x) for x in torch.__version__.split("+")[0].split(".")[:2])
    if (mj, mn) < (2, 7):
        print(f"[WARN] SAM3 recommends PyTorch 2.7+ (current {torch.__version__}).")
    return True


def load_model():
    """Load the SAM3 image model -> sets/returns the module-level processor."""
    global _PROCESSOR
    if _PROCESSOR is not None:
        return _PROCESSOR
    ensure_source()
    ckpt_file = resolve_checkpoint()

    from sam3.model_builder import build_sam3_image_model
    from sam3.model.sam3_image_processor import Sam3Processor

    print(f"[*] loading checkpoint: {ckpt_file}")
    model = build_sam3_image_model(
        checkpoint_path=str(ckpt_file), load_from_HF=False,
        device="cuda", eval_mode=True,
    )
    _PROCESSOR = Sam3Processor(model)
    n = sum(p.numel() for p in model.parameters()) / 1e6
    print(f"[OK] SAM3 loaded ({n:.0f}M params, {next(model.parameters()).device})")
    return _PROCESSOR


def analyze_frame(pil_image, prompt: str | None = None, score_th: float | None = None):
    """Single frame -> (person_count, boxes[N,4], density_mask[H,W])."""
    import torch
    import numpy as np
    from .config import PROMPT, SCORE_TH

    prompt = PROMPT if prompt is None else prompt
    score_th = SCORE_TH if score_th is None else score_th

    proc = load_model()
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        state = proc.set_image(pil_image)
        out = proc.set_text_prompt(state=state, prompt=prompt)

    def np_(x):
        return x.detach().float().cpu().numpy() if torch.is_tensor(x) else np.asarray(x)

    masks, boxes, scores = np_(out["masks"]), np_(out["boxes"]), np_(out["scores"])
    keep = scores >= score_th
    masks, boxes = masks[keep], boxes[keep]
    if masks.ndim == 4:
        masks = masks[:, 0]
    masks_bin = masks > 0.5
    H, W = pil_image.size[1], pil_image.size[0]
    density = np.zeros((H, W), np.float32)
    for m in masks_bin:
        density += m.astype(np.float32)
    return int(keep.sum()), boxes, density
