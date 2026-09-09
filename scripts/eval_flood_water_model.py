"""학습된 침수 세그멘테이션 모델 검증 — 기존 모델이 실패한 지점을 통과하는지 확인.

`docs/flood_water_dataset_workflow.md` 2-4절의 "추가 필수 검증" 3항목을 자동으로
돌린다. 기존 Ultralytics `models/best.pt`는 아래 ①②에서 실패했으므로, 새 모델이
이를 통과하는지가 전환 성공의 실질적 판정 기준이다.

  ① 실제 침수 영상에서 물을 검출하는가        (기존 모델: 0건 검출 = 실패)
  ② 레터박스 영상의 검은 여백을 물로 오인하는가 (기존 모델: 19% 오탐 = 실패)
  ③ 맑은 날 CCTV에서 오탐이 없는가            (기존 모델: 통과)

Usage:
    python scripts/eval_flood_water_model.py
    python scripts/eval_flood_water_model.py --ckpt <경로> --save-overlays
"""
from __future__ import annotations

import argparse
import glob
from pathlib import Path

import cv2
import numpy as np
import torch
from torchvision.models.segmentation import (deeplabv3_mobilenet_v3_large,
                                             lraspp_mobilenet_v3_large)

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import crop_letterbox, detect_letterbox

DEFAULT_CKPT = (PROJECT_ROOT / "data" / "datasets" / "flood_water_own" /
                "runs" / "flood_lraspp_384" / "best.pt")
OUT_DIR = PROJECT_ROOT / "data" / "road_proto" / "eval_flood"


def load_model(ckpt_path: Path):
    ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    arch = ck.get("arch", "lraspp")
    size = int(ck.get("size", 384))
    builder = lraspp_mobilenet_v3_large if arch == "lraspp" else deeplabv3_mobilenet_v3_large
    model = builder(weights=None, num_classes=2)
    model.load_state_dict(ck["model"])
    model.eval()
    print(f"[eval] 체크포인트: {ckpt_path}")
    print(f"[eval]   arch={arch} size={size} "
          f"학습 val F1={ck.get('val_f1', float('nan')):.4f} IoU={ck.get('val_iou', float('nan')):.4f} "
          f"(epoch {ck.get('epoch','?')})")
    return model, size


@torch.no_grad()
def predict_ratio(model, frame: np.ndarray, size: int) -> tuple[float, np.ndarray]:
    """(물 비율 0~1, 원본 크기 이진 마스크)"""
    h, w = frame.shape[:2]
    x = cv2.resize(frame, (size, size))
    x = torch.from_numpy(x.transpose(2, 0, 1).astype(np.float32) / 255.0).unsqueeze(0)
    pred = model(x)["out"].argmax(1)[0].numpy().astype(np.uint8)
    mask = cv2.resize(pred, (w, h), interpolation=cv2.INTER_NEAREST)
    return float(mask.mean()), mask


def overlay(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    ov = frame.copy()
    ov[mask > 0] = (0, 0, 255)
    return cv2.addWeighted(frame, 0.6, ov, 0.4, 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ckpt", default=str(DEFAULT_CKPT))
    ap.add_argument("--save-overlays", action="store_true", help="오버레이 이미지 저장")
    args = ap.parse_args()

    ckpt = Path(args.ckpt)
    if not ckpt.exists():
        raise SystemExit(f"체크포인트가 없습니다: {ckpt}\n먼저 train_flood_water_cpu.py로 학습하세요.")
    model, size = load_model(ckpt)
    if args.save_overlays:
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    verdicts = []

    # ① 실제 침수 영상 (레터박스 제거본) - 물을 검출해야 통과
    print("\n[① 침수 영상 검출]  기대: 물 비율이 뚜렷하게 나와야 함 (기존 모델 0% = 실패)")
    frames = sorted(glob.glob(str(PROJECT_ROOT / "data/datasets/flood_water_own/raw/underpath_flood1/*.jpg")))
    ratios = []
    for p in frames:
        f = cv2.imread(p)
        r, m = predict_ratio(model, f, size)
        ratios.append(r)
        if args.save_overlays:
            cv2.imwrite(str(OUT_DIR / f"flood_{Path(p).stem}.jpg"), overlay(f, m))
    if ratios:
        avg = float(np.mean(ratios)) * 100
        ok = avg >= 5.0
        print(f"   프레임 {len(ratios)}장 평균 물비율 {avg:.1f}%  -> {'PASS' if ok else 'FAIL'}")
        verdicts.append(("① 침수 영상 검출", ok))
    else:
        print("   (검증 프레임 없음 - extract_flood_frames.py 먼저 실행)")

    # ② 레터박스 - 전처리(크롭) 유무를 둘 다 측정해 "모델 문제 vs 전처리 문제"를 구분
    print("\n[② 레터박스 오탐]  기대: 검은 여백을 물로 잡지 않아야 함 (기존 모델 19% = 실패)")
    vid = PROJECT_ROOT / "data/samples/flood/underpath_flood1.mp4"
    if vid.exists():
        cap = cv2.VideoCapture(str(vid))
        ok_read, frame = cap.read()
        cap.release()
        if ok_read:
            # 임의의 "좌우 18%"가 아니라 detect_letterbox가 찾아낸 **실제 여백 영역**만
            # 측정한다. (크롭 후에는 그 영역이 실제 도로/침수 구간이라, 고정 비율로
            # 재면 정상 검출을 오탐으로 오판하게 된다 - 초기 테스트의 실수)
            t, b, l, r = detect_letterbox(frame)
            h, w = frame.shape[:2]
            _, m_raw = predict_ratio(model, frame, size)

            bars = []
            if l > 0:
                bars.append(m_raw[:, :l])
            if r < w:
                bars.append(m_raw[:, r:])
            if t > 0:
                bars.append(m_raw[:t, :])
            if b < h:
                bars.append(m_raw[b:, :])

            if bars:
                bar_ratio = float(np.mean([x.mean() for x in bars])) * 100
                ok = bar_ratio < 5.0
                print(f"   감지된 여백: 좌{l}px 우{w - r}px 상{t}px 하{h - b}px")
                print(f"   여백 영역 내 물 오탐 {bar_ratio:.1f}%  -> {'PASS' if ok else 'FAIL'}")
                if not ok:
                    print("   → 전처리 crop_letterbox()로 여백을 제거하면 이 영역 자체가 사라짐")
            else:
                bar_ratio, ok = 0.0, True
                print("   (이 영상에 레터박스 없음 - 검사 생략)")

            if args.save_overlays:
                cv2.imwrite(str(OUT_DIR / "letterbox_raw.jpg"), overlay(frame, m_raw))
                cropped = crop_letterbox(frame)
                _, m_crop = predict_ratio(model, cropped, size)
                cv2.imwrite(str(OUT_DIR / "letterbox_cropped.jpg"), overlay(cropped, m_crop))
            verdicts.append(("② 레터박스 여백 오탐", ok))
    else:
        print(f"   (영상 없음: {vid})")

    # ③ 맑은 날 CCTV - 오탐이 없어야 통과
    print("\n[③ 맑은 날 오탐]  기대: 물이 거의 검출되지 않아야 함 (기존 모델 통과)")
    dry = sorted(glob.glob(str(PROJECT_ROOT / "data/datasets/road_cctv_own/raw/*/*.jpg")))
    if dry:
        rs = []
        for p in dry:
            f = cv2.imread(p)
            if f is None:
                continue
            r, m = predict_ratio(model, f, size)
            rs.append(r)
            if args.save_overlays and r > 0.02:
                cv2.imwrite(str(OUT_DIR / f"dry_fp_{Path(p).parent.name}.jpg"), overlay(f, m))
        avg = float(np.mean(rs)) * 100
        worst = float(np.max(rs)) * 100
        ok = avg < 3.0
        print(f"   {len(rs)}장 평균 {avg:.2f}% / 최대 {worst:.2f}%  -> {'PASS' if ok else 'FAIL'}")
        verdicts.append(("③ 맑은 날 오탐", ok))
    else:
        print("   (맑은 날 프레임 없음 - collect_road_cctv_frames.py 먼저 실행)")

    print("\n" + "=" * 52)
    for name, ok in verdicts:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    n_pass = sum(1 for _, ok in verdicts if ok)
    print(f"  종합: {n_pass}/{len(verdicts)} 통과")
    if args.save_overlays:
        print(f"  오버레이: {OUT_DIR}")


if __name__ == "__main__":
    main()
