"""Crowd (SAM3) CLI pipeline — image/video analysis commands.

Ported from SAM's ``SAM3_install.py`` CLI commands. **GPU-only** (see
``sam3_model.py``'s module docstring) — not executable or tested in this
session. Preserved faithfully from the original so it is ready to verify on
an actual CUDA machine (docs/integration_plan.md section 6).

Run (after ``pip install -e ".[crowd-gpu]"`` and placing ``sam3.pt`` per
``crowd.config.resolve_checkpoint``'s search locations):
  python -m tot_dashboard.crowd.sam3_pipeline setup
  python -m tot_dashboard.crowd.sam3_pipeline image   --input crowd.jpg
  python -m tot_dashboard.crowd.sam3_pipeline pipeline --input crowd.mp4
  python -m tot_dashboard.crowd.sam3_pipeline grid-image --input crowd.jpg
  python -m tot_dashboard.crowd.sam3_pipeline grid-video --input crowd.mp4
"""
from __future__ import annotations

import argparse
import os
from collections import Counter

from .behavior_tracker import CrowdBehaviorTracker
from .config import ALERT_DENSITY, FRAME_STRIDE, OUTPUT_DIR, ensure_output_dir
from .heatmap import density_to_heatmap, grid_density, make_grid, overlay_heatmap
from .preemptive_action import PreemptiveAction
from .sam3_model import analyze_frame, check_env, ensure_deps, ensure_source, load_model
from .semantic_risk_agent import SemanticRiskAgent
from .vlm_situation import LAST_VLM, vlm_interpret


def _set_korean_font() -> None:
    try:
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Malgun Gothic"  # Windows default Korean font
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:  # noqa: BLE001
        pass


def run_image(path: str, show: bool = False) -> None:
    """Image segmentation + density + grid heatmap."""
    import numpy as np
    import cv2
    from PIL import Image
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _set_korean_font()
    ensure_output_dir()

    assert os.path.exists(path), f"image not found: {path}"
    image = Image.open(path).convert("RGB")
    W, H = image.size
    count, boxes, density = analyze_frame(image)
    crowd_pct = float((density > 0).sum()) / (H * W) * 100
    print(f"[OK] detected: {count} persons | occupied area: {crowd_pct:.1f}%")

    grid, (gc, gr, gcell) = grid_density(boxes, W, H)
    heat_bgr, _ = density_to_heatmap(grid, W, H)
    img_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    overlay = overlay_heatmap(img_bgr, heat_bgr)

    p_overlay = os.path.join(OUTPUT_DIR, "grid_density_image.png")
    p_pure = os.path.join(OUTPUT_DIR, "grid_density_pure.png")
    cv2.imwrite(p_overlay, overlay)
    cv2.imwrite(p_pure, heat_bgr)

    fig, ax = plt.subplots(1, 3, figsize=(22, 7))
    ax[0].imshow(image); ax[0].set_title(f"입력 ({count}명, {W}x{H})"); ax[0].axis("off")
    ax[1].imshow(cv2.cvtColor(heat_bgr, cv2.COLOR_BGR2RGB))
    ax[1].set_title(f"격자 밀집 히트맵 {gc}x{gr} (빨강=고밀도)"); ax[1].axis("off")
    ax[2].imshow(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB))
    ax[2].set_title("원본 오버레이"); ax[2].axis("off")
    plt.tight_layout()
    p_fig = os.path.join(OUTPUT_DIR, "image_result.png")
    plt.savefig(p_fig, dpi=150, bbox_inches="tight")
    if show:
        plt.show()
    print("[OK] saved:", p_overlay, "|", p_pure, "|", p_fig)


def run_pipeline(path: str) -> None:
    """Full 5-stage pipeline -> annotated video + alerts.json + risk timeline."""
    import numpy as np
    import cv2
    from PIL import Image
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _set_korean_font()
    ensure_output_dir()

    assert os.path.exists(path), f"video not found: {path}"
    load_model()
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"video {W}x{H}, {fps:.1f}fps, {total} frames -> analyzing every {FRAME_STRIDE} frames")

    tracker = CrowdBehaviorTracker(fps=fps)
    agent = SemanticRiskAgent()
    action = PreemptiveAction()
    out_video = os.path.join(OUTPUT_DIR, "risk_annotated.mp4")
    writer = cv2.VideoWriter(out_video, cv2.VideoWriter_fourcc(*"mp4v"),
                             max(fps / FRAME_STRIDE, 1), (W, H))
    SEV_COLOR = {0: (0, 200, 0), 1: (0, 200, 200), 2: (0, 160, 255),
                 3: (0, 80, 255), 4: (0, 0, 255)}
    timeline = []
    vlm_src = Counter()
    idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % FRAME_STRIDE != 0:
            idx += 1
            continue
        t_sec = idx / fps
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        count, boxes, density = analyze_frame(pil)
        density_pct = float((density > 0).sum()) / (H * W) * 100
        summary = {"count": count, "density_pct": density_pct}
        behavior, det = tracker.update(boxes, np.ones(len(boxes), np.float32), t_sec)
        wtext, src = vlm_interpret(pil, summary, behavior)
        vlm_src[src] += 1
        risk = agent.assess(summary, behavior, wtext)
        payload = action.act(t_sec, idx, risk, density, vlm_full=LAST_VLM)
        timeline.append((t_sec, count, density_pct, risk["score"], risk["severity"]))

        color = SEV_COLOR[risk["severity"]]
        if density.max() > 0:
            heat = cv2.applyColorMap((density / density.max() * 255).astype(np.uint8), cv2.COLORMAP_JET)
            frame = cv2.addWeighted(frame, 0.75, heat, 0.25, 0)
        for xyxy in (det.xyxy if len(det) else []):
            x0, y0, x1, y1 = map(int, xyxy)
            cv2.rectangle(frame, (x0, y0), (x1, y1), (255, 255, 255), 1)
        if payload["hotspot"]:
            cv2.circle(frame, (payload["hotspot"]["x"], payload["hotspot"]["y"]), 18, color, 3)
        cv2.rectangle(frame, (0, 0), (W, 72), (0, 0, 0), -1)
        cv2.putText(frame, f"[{risk['risk_name_kr']}] score={risk['score']:.2f} | {count}p dens={density_pct:.0f}%",
                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.putText(frame, f"surge={behavior['surge']:.2f} disp={behavior['dispersion']:.2f} div={behavior['divergence']:+.1f} [{src}]",
                    (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 1)
        if payload["alert"]:
            cv2.putText(frame, "!! ALERT !!", (W - 220, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
        writer.write(frame)
        tag = "[ALERT]" if payload["alert"] else "       "
        print(f"{tag} t={t_sec:5.1f}s f{idx:5d} | {count:3d}p dens{density_pct:4.0f}% | "
              f"{risk['risk_name_kr']:6s} score={risk['score']:.2f} | VLM:{src}")
        idx += 1

    cap.release()
    writer.release()
    json_path = action.save(os.path.join(OUTPUT_DIR, "alerts.json"))

    ts = np.array([r[0] for r in timeline]); cnt = np.array([r[1] for r in timeline])
    dens = np.array([r[2] for r in timeline]); scr = np.array([r[3] for r in timeline])
    sev = np.array([r[4] for r in timeline])
    fig, ax = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    ax[0].plot(ts, cnt, color="steelblue"); ax[0].set_ylabel("인원수"); ax[0].set_title("① 군중 인원 추이"); ax[0].grid(alpha=0.3)
    ax[1].plot(ts, dens, color="darkorange"); ax[1].axhline(ALERT_DENSITY, ls="--", c="r")
    ax[1].set_ylabel("밀집도(%)"); ax[1].set_title("② 군중 밀집도 추이"); ax[1].grid(alpha=0.3)
    ax[2].plot(ts, scr, color="crimson", lw=2); ax[2].fill_between(ts, scr, where=sev >= 3, color="red", alpha=0.3)
    ax[2].set_ylim(0, 1); ax[2].set_ylabel("위험점수"); ax[2].set_xlabel("시간(초)")
    ax[2].set_title("③ 종합 위험점수"); ax[2].grid(alpha=0.3)
    plt.tight_layout()
    tl = os.path.join(OUTPUT_DIR, "risk_timeline.png")
    plt.savefig(tl, dpi=150, bbox_inches="tight")

    print(f"\n[OK] done | alerts {len(action.alert_log)}")
    print("  VLM usage:", dict(vlm_src))
    print("  annotated video:", out_video)
    print("  alert log:", json_path)
    print("  timeline:", tl)


def run_grid_video(path: str, normalize_max: float | None = None) -> None:
    """Grid density heatmap video (secondary output)."""
    import numpy as np
    import cv2
    from PIL import Image

    assert os.path.exists(path), f"video not found: {path}"
    ensure_output_dir()
    load_model()
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    gc, gr, gcell = make_grid(W, H)
    print(f"video {W}x{H} -> grid {gc}x{gr} (cell {gcell:.1f}px)")
    out = os.path.join(OUTPUT_DIR, "grid_heatmap.mp4")
    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), max(fps / FRAME_STRIDE, 1), (W, H))
    idx = processed = 0
    peak = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % FRAME_STRIDE != 0:
            idx += 1
            continue
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        cnt, boxes, _ = analyze_frame(pil)
        grid, _ = grid_density(boxes, W, H)
        heat, _ = density_to_heatmap(grid, W, H, normalize_max=normalize_max)
        o = overlay_heatmap(frame, heat)
        cv2.rectangle(o, (0, 0), (W, 40), (0, 0, 0), -1)
        cv2.putText(o, f"Grid {gc}x{gr} | {cnt} persons | RED=high density",
                    (10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        writer.write(o)
        peak = max(peak, float(grid.max()))
        processed += 1
        if processed % 10 == 0:
            print(f"  processed {processed} frames (t={idx / fps:.1f}s, {cnt} persons)")
        idx += 1
    cap.release()
    writer.release()
    print(f"[OK] {processed} frames | peak={peak:.2f} | saved: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="SAM3 crowd risk detection (local GPU)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="install deps + clone source + verify")
    p = sub.add_parser("image", help="analyze an image")
    p.add_argument("--input", required=True)
    p.add_argument("--show", action="store_true")
    p = sub.add_parser("pipeline", help="full 5-stage pipeline (video)")
    p.add_argument("--input", required=True)
    p = sub.add_parser("grid-image", help="grid heatmap (image)")
    p.add_argument("--input", required=True)
    p.add_argument("--show", action="store_true")
    p = sub.add_parser("grid-video", help="grid heatmap (video)")
    p.add_argument("--input", required=True)
    p.add_argument("--norm", type=float, default=None)
    args = ap.parse_args()

    if args.cmd == "setup":
        ensure_deps()
        check_env()
        ensure_source()
        print("\n[OK] setup complete. Place sam3.pt (see crowd.config.resolve_checkpoint) "
              "then run image/pipeline.")
    elif args.cmd == "image":
        check_env(); run_image(args.input, show=args.show)
    elif args.cmd == "grid-image":
        check_env(); run_image(args.input, show=args.show)
    elif args.cmd == "pipeline":
        check_env(); run_pipeline(args.input)
    elif args.cmd == "grid-video":
        check_env(); run_grid_video(args.input, normalize_max=args.norm)
    else:
        ap.print_help()
        print("\ne.g. python -m tot_dashboard.crowd.sam3_pipeline setup")
        print("     python -m tot_dashboard.crowd.sam3_pipeline pipeline --input crowd.mp4")


if __name__ == "__main__":
    main()
