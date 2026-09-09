"""Phase 2 프로토타입 검증용 — 도로 노면 손상(포트홀/균열) 탐지 1회 실행.

road/defect_detection.py의 detect_defects()를 실제 프레임 한 장에 돌려보고,
박스를 그려 저장한다. flood 도메인의 scripts/roi_editor.py와 동일한 3가지
프레임 소스(--video/--frame/--block)를 지원한다.

⚠ 이 스크립트는 학습된 체크포인트(--model)가 있어야 의미 있는 결과가 나온다.
docs/road_surface_management_plan.md 2장 참고 — 공개 데이터셋으로 학습한
체크포인트가 아직 없다면, COCO 사전학습 모델(models/yolo11s.pt 등)로 실행해도
포트홀/균열 클래스가 없어 아무것도 검출되지 않는다(정상 동작).

Usage:
    python scripts/test_road_defect_detection.py --model <checkpoint.pt> --block BLOCK-CHORYANG
    python scripts/test_road_defect_detection.py --model <checkpoint.pt> --frame path/to/image.jpg
    python scripts/test_road_defect_detection.py --model <checkpoint.pt> --video path/to/video.mp4
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.models_loader import load_model
from tot_dashboard.common.video_io import read_first_frame
from tot_dashboard.road.defect_detection import detect_defects

_BOX_COLOR = {"pothole": (0, 0, 229), "crack": (23, 168, 212)}  # BGR


def _grab_hls_frame(url: str, timeout_tries: int = 30):
    cap = cv2.VideoCapture(url)
    frame = None
    if cap.isOpened():
        for _ in range(timeout_tries):
            ok, f = cap.read()
            if ok:
                frame = f
                break
        cap.release()
    return frame


def _grab_frame(args):
    if args.frame:
        img = cv2.imread(args.frame)
        if img is None:
            raise SystemExit(f"cannot read frame image: {args.frame}")
        return img, Path(args.frame).stem
    if args.video:
        img = read_first_frame(args.video)
        if img is None:
            raise SystemExit(f"cannot read first frame of video: {args.video}")
        return img, Path(args.video).stem
    if args.block:
        path = PROJECT_ROOT / "configs" / "blocks.json"
        blocks = json.loads(path.read_text(encoding="utf-8"))["blocks"]
        block = next((b for b in blocks if b["id"] == args.block), None)
        if block is None:
            raise SystemExit(f"block id not found in configs/blocks.json: {args.block}")
        url = (block.get("source") or {}).get("url")
        if not url:
            raise SystemExit(f"block {args.block} has no source.url (synthetic block?)")
        print(f"[test_road_defect_detection] grabbing frame from HLS: {url}")
        img = _grab_hls_frame(url)
        if img is None:
            raise SystemExit(f"could not connect to {url} -- try --frame with a saved still image instead")
        return img, block["id"]
    raise SystemExit("one of --video, --frame, or --block is required")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="path to a YOLO detection checkpoint (.pt)")
    ap.add_argument("--video", default=None)
    ap.add_argument("--frame", default=None)
    ap.add_argument("--block", default=None, help="configs/blocks.json block id (live HLS)")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--out", default=None, help="annotated output path (default: data/road_proto/<name>_annotated.jpg)")
    args = ap.parse_args()

    frame, name = _grab_frame(args)
    h, w = frame.shape[:2]
    print(f"[test_road_defect_detection] frame size: {w}x{h}")

    model = load_model(args.model, device="cpu")
    print(f"[test_road_defect_detection] model classes: {dict(model.names)}")

    result = detect_defects(model, frame, conf=args.conf)
    print(f"[test_road_defect_detection] detected {result.count} defect(s):")
    for d in result.defects:
        print(f"  - {d.cls_name} conf={d.confidence:.2f} box={d.box}")

    annotated = frame.copy()
    for d in result.defects:
        x1, y1, x2, y2 = d.box
        color = _BOX_COLOR.get(d.cls_name, (0, 200, 0))
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        cv2.putText(annotated, f"{d.cls_name} {d.confidence:.2f}", (x1, max(0, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

    out_path = Path(args.out) if args.out else PROJECT_ROOT / "data" / "road_proto" / f"{name}_annotated.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), annotated)
    print(f"[test_road_defect_detection] saved: {out_path}")


if __name__ == "__main__":
    main()
