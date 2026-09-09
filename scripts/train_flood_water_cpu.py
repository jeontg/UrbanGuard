"""침수 물 영역 시맨틱 세그멘테이션 학습 — **GPU 없이 CPU에서 수행**.

## 왜 이 방식인가

기존 물 세그멘테이션은 Ultralytics YOLO11-seg(**인스턴스** 세그멘테이션, AGPL-3.0)를
썼다. 그런데 파이프라인 코드를 확인해보니 ``WaterResult.num_instances``는 어디에서도
판단에 쓰이지 않고, 모든 지표(``water_area_pixels``/``water_area_ratio``/ROI 교집합)가
**이진 마스크 하나**만 필요로 한다(``flood/metrics_core.py``).

즉 인스턴스 분리가 불필요하므로 **시맨틱 세그멘테이션으로 충분**하며, 이 전환은
두 문제를 동시에 푼다:

1. **라이선스** — torchvision 세그멘테이션 모델은 **BSD 라이선스**라 AGPL 의무가 없다
2. **GPU 부재** — 경량 모델(LR-ASPP MobileNetV3)은 CPU로도 학습 가능

실측(본 개발 PC, 12스레드 CPU, 학습 1,200장 기준):

| 모델 | 입력크기 | 1 epoch | 40 epoch |
|---|---|---|---|
| **LR-ASPP MobileNetV3** | 384 | 4.6분 | **약 3.0시간** |
| DeepLabV3 MobileNetV3 | 384 | 7.2분 | 약 4.8시간 |
| LR-ASPP MobileNetV3 | 512 | 7.8분 | 약 5.2시간 |

## 입력 데이터

``data/datasets/flood_water_own/labeled/<세트>/{images,labels}`` 의 YOLO-seg 폴리곤
라벨을 읽어 이진 마스크로 래스터화한다. 자체 라벨링분
(``scripts/label_flood_water.py``)과 공개 데이터 변환분
(``scripts/convert_flood_masks_to_yolo.py``)이 같은 포맷이라 그대로 섞어 쓸 수 있다.

## 평가 지표에 대한 주의

기존 모델의 기준선 ``mask mAP50 0.864``는 **인스턴스** 세그멘테이션 지표라 시맨틱
세그멘테이션과 직접 비교할 수 없다. 여기서는 **IoU / F1(Dice)** 를 쓴다. 참고로 이
데이터셋(HydroShare)을 만든 원 논문도 시맨틱 세그멘테이션으로 **F1 0.9 이상**을
보고했으므로, 그 수준이 현실적인 목표치다.

Usage:
    python scripts/train_flood_water_cpu.py --epochs 40
    python scripts/train_flood_water_cpu.py --arch deeplabv3 --size 384 --epochs 30
    python scripts/train_flood_water_cpu.py --sets hydroshare_webcoos busan_rain_auto
"""
from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
from torchvision.models.segmentation import (deeplabv3_mobilenet_v3_large,
                                             lraspp_mobilenet_v3_large)

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import resolve_dataset
from tot_dashboard.common.video_io import IMAGE_EXTS, imread_unicode

# ★ 2026-08-25 — 2026-08-14에 라벨 데이터(1,570장)를 D:\dev-PoC_DATA로 옮긴
#   뒤로 이 상수가 실제로는 빈 폴더를 가리키고 있었다(학습 화면 신설 중
#   발견). resolve_dataset()이 프로젝트 폴더를 먼저 보고, 없으면 보관소로
#   물러난다 — 보관소 데이터를 복사해 오지 않는다(사용자 확인).
LABELED_ROOT = resolve_dataset("flood_labeled") or (
    PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "labeled")
OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "runs"


def polygons_to_mask(label_path: Path, w: int, h: int) -> np.ndarray:
    """YOLO-seg 폴리곤 라벨 -> 0/1 이진 마스크. 라벨이 없거나 비면 전부 0(배경)."""
    mask = np.zeros((h, w), np.uint8)
    if not label_path.exists():
        return mask
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        v = [float(x) for x in parts[1:]]
        pts = np.array([[v[i] * w, v[i + 1] * h] for i in range(0, len(v) - 1, 2)], np.int32)
        if len(pts) >= 3:
            cv2.fillPoly(mask, [pts], 1)
    return mask


class FloodSegDataset(Dataset):
    def __init__(self, pairs: list[tuple[Path, Path]], size: int, augment: bool):
        self.pairs = pairs
        self.size = size
        self.augment = augment

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, i):
        ip, lp = self.pairs[i]
        # cv2.imread 대신 imread_unicode — 학습 데이터 보관소 경로
        # (D:\dev-PoC_DATA\02_학습데이터_침수\...)의 폴더명 자체가 한글이라
        # cv2.imread 는 Windows에서 예외 없이 None 을 반환한다(2026-08-25
        # 실사용 중 발견 — 전 이미지가 조용히 검은 화면으로 대체되고
        # 있었다).
        img = imread_unicode(ip)
        if img is None:
            print(f"[train] ⚠ 이미지를 읽지 못했습니다(검은 화면으로 대체): {ip}")
            img = np.zeros((self.size, self.size, 3), np.uint8)
        h, w = img.shape[:2]
        mask = polygons_to_mask(lp, w, h)

        img = cv2.resize(img, (self.size, self.size))
        mask = cv2.resize(mask, (self.size, self.size), interpolation=cv2.INTER_NEAREST)

        if self.augment:
            if random.random() < 0.5:                       # 좌우 반전
                img, mask = img[:, ::-1].copy(), mask[:, ::-1].copy()
            if random.random() < 0.3:                       # 밝기 변화(주야/조도 대응)
                f = random.uniform(0.6, 1.4)
                img = np.clip(img.astype(np.float32) * f, 0, 255).astype(np.uint8)

        x = torch.from_numpy(img.transpose(2, 0, 1).astype(np.float32) / 255.0)
        y = torch.from_numpy(mask.astype(np.int64))
        return x, y


def collect_pairs(sets: list[str] | None, root: Path = LABELED_ROOT) -> list[tuple[Path, Path]]:
    if not root.is_dir():
        raise SystemExit(f"라벨 폴더가 없습니다: {root}")
    dirs = ([root / s for s in sets] if sets
            else sorted(d for d in root.iterdir() if d.is_dir()))
    pairs = []
    for d in dirs:
        img_dir, lbl_dir = d / "images", d / "labels"
        if not img_dir.is_dir():
            print(f"[train] 건너뜀(폴더 없음): {d}")
            continue
        n = 0
        for ip in sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS):
            pairs.append((ip, lbl_dir / f"{ip.stem}.txt"))
            n += 1
        print(f"[train] {d.name}: {n}장")
    return pairs


@torch.no_grad()
def evaluate(model, loader) -> tuple[float, float]:
    """(IoU, F1) — 물(전경) 클래스 기준."""
    model.eval()
    inter = union = tp = fp = fn = 0
    for x, y in loader:
        pred = model(x)["out"].argmax(1)
        p, t = pred == 1, y == 1
        inter += int((p & t).sum())
        union += int((p | t).sum())
        tp += int((p & t).sum())
        fp += int((p & ~t).sum())
        fn += int((~p & t).sum())
    iou = inter / union if union else 1.0
    f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 1.0
    return iou, f1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", nargs="*", default=None,
                    help="사용할 라벨 세트 이름들(생략 시 labeled/ 전체)")
    ap.add_argument("--arch", choices=["lraspp", "deeplabv3"], default="lraspp",
                    help="lraspp=가장 빠름(권장), deeplabv3=조금 더 정확·느림")
    ap.add_argument("--size", type=int, default=384)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--threads", type=int, default=12, help="CPU 스레드 수")
    ap.add_argument("--name", default=None, help="실행 이름(기본: arch_size)")
    ap.add_argument("--data-root", default=None,
                    help="라벨 폴더 경로(생략 시 프로젝트→보관소 순으로 자동 탐색)")
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    random.seed(0)
    torch.manual_seed(0)

    data_root = Path(args.data_root) if args.data_root else LABELED_ROOT
    pairs = collect_pairs(args.sets, data_root)
    if not pairs:
        raise SystemExit("학습할 데이터가 없습니다. convert_flood_masks_to_yolo.py 또는 "
                         "label_flood_water.py로 라벨을 먼저 만드세요.")
    random.shuffle(pairs)
    n_val = max(1, int(len(pairs) * args.val_frac))
    val_pairs, train_pairs = pairs[:n_val], pairs[n_val:]
    print(f"[train] 총 {len(pairs)}장 -> train {len(train_pairs)} / val {len(val_pairs)}")

    train_ds = FloodSegDataset(train_pairs, args.size, augment=True)
    val_ds = FloodSegDataset(val_pairs, args.size, augment=False)
    # num_workers=0: Windows에서 spawn 오버헤드가 커 CPU 학습에는 오히려 불리
    train_dl = DataLoader(train_ds, batch_size=args.batch, shuffle=True, num_workers=0)
    val_dl = DataLoader(val_ds, batch_size=args.batch, shuffle=False, num_workers=0)

    builder = lraspp_mobilenet_v3_large if args.arch == "lraspp" else deeplabv3_mobilenet_v3_large
    # weights_backbone: ImageNet 사전학습 백본(BSD/Apache 계열, AGPL 아님)으로 수렴 가속
    model = builder(weights=None, weights_backbone="DEFAULT", num_classes=2)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    lossf = nn.CrossEntropyLoss()

    run = OUT_ROOT / (args.name or f"{args.arch}_{args.size}")
    run.mkdir(parents=True, exist_ok=True)
    csv = run / "results.csv"
    csv.write_text("epoch,train_loss,val_iou,val_f1,minutes\n", encoding="utf-8")

    best = -1.0
    t_start = time.time()
    for ep in range(1, args.epochs + 1):
        model.train()
        tot = nb = 0
        t0 = time.time()
        for x, y in train_dl:
            out = model(x)["out"]
            loss = lossf(out, y)
            loss.backward()
            opt.step()
            opt.zero_grad()
            tot += loss.item()
            nb += 1
        sched.step()
        iou, f1 = evaluate(model, val_dl)
        mins = (time.time() - t_start) / 60
        line = f"{ep},{tot / max(nb,1):.4f},{iou:.4f},{f1:.4f},{mins:.1f}"
        csv.write_text(csv.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
        print(f"[train] epoch {ep}/{args.epochs}  loss {tot / max(nb,1):.4f}  "
              f"val IoU {iou:.4f}  F1 {f1:.4f}  ({(time.time() - t0) / 60:.1f}분/epoch, 누적 {mins:.0f}분)")

        if f1 > best:
            best = f1
            torch.save({"model": model.state_dict(), "arch": args.arch,
                        "size": args.size, "val_f1": f1, "val_iou": iou,
                        "epoch": ep, "license_note": "torchvision(BSD) 기반 - AGPL 아님"},
                       run / "best.pt")
    print(f"\n[train] 완료 - 최고 val F1 {best:.4f} -> {run / 'best.pt'}")
    print(f"[train] 목표 참고: 원 논문(HydroShare 데이터셋) 시맨틱 세그멘테이션 F1 0.9 이상")


if __name__ == "__main__":
    main()
