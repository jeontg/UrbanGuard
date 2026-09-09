"""인파관리 — 사람검출(Faster R-CNN) 자체 학습. 관리자 화면(신규)에서 쓰는 진입점.

## 현재 상태 — 아직 자체 학습 데이터가 없다 (2026-08-25 확인)

지금 운영 중인 사람검출(``crowd/live_analyzer.py``의 ``DetectorCrowdSource``)은
torchvision COCO 사전학습 Faster R-CNN을 **파인튜닝 없이 그대로** 쓴다. 게다가
실제 운영은 아직 이 검출기가 아니라 ``mock``(합성 궤적) 모드로 돈다 — 「4대
탐지 기능 기술 정리」(2026-08-25) §2-3 참고.

## 왜 YOLO 라벨 형식을 쓰는가

침수·도로 노면·교통과 **같은 라벨링 형식**(YOLO 상자 텍스트, 클래스 0=person)
으로 통일해, 라벨링 도구·작업 방식을 도메인마다 새로 배우지 않게 한다.
실제 검출 모델(torchvision Faster R-CNN)은 이 텍스트 라벨을 상자 좌표로
변환해서 쓸 뿐, 저장 형식 자체는 다른 3개 도메인과 같다.

## 데이터가 준비되면

``data/datasets/crowd_person_own/yolo/`` (또는 학습 데이터 보관소
``D:\\dev-PoC_DATA\\08_학습데이터_인파\\crowd_person_own\\yolo\\``)에
``images/``·``labels/`` 를 YOLO 형식(단일 클래스 person, id 0)으로 채워
넣으면 이 스크립트가 그대로 읽는다.

## 평가 지표

COCO mAP 같은 표준 도구를 새로 들이는 대신, 침수 학습 스크립트와 같은 방식
(자체 IoU 매칭)으로 **재현율(recall)·정밀도(precision)**을 직접 계산한다 —
신뢰도 임계값 0.5, IoU 임계값 0.5 기준. F1이 가장 높은 체크포인트를 저장한다.

Usage:
    python scripts/train_crowd_person_frcnn.py --epochs 20
"""
from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import dataset_hint, resolve_dataset
from tot_dashboard.common.video_io import IMAGE_EXTS, imread_unicode

RUNS = PROJECT_ROOT / "data" / "training_runs" / "crowd"


def yolo_boxes(label_path: Path, w: int, h: int) -> np.ndarray:
    """YOLO 상자 라벨(cx,cy,w,h, 0~1) -> 픽셀 xyxy. 라벨 없으면 빈 배열."""
    if not label_path.exists():
        return np.zeros((0, 4), np.float32)
    out = []
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        _, cx, cy, bw, bh = (float(x) for x in parts[:5])
        x1, y1 = (cx - bw / 2) * w, (cy - bh / 2) * h
        x2, y2 = (cx + bw / 2) * w, (cy + bh / 2) * h
        out.append([max(0, x1), max(0, y1), min(w, x2), min(h, y2)])
    return np.array(out, np.float32) if out else np.zeros((0, 4), np.float32)


def collect_pairs(root: Path) -> list[tuple[Path, Path]]:
    img_dir, lbl_dir = root / "images", root / "labels"
    if not img_dir.is_dir():
        raise SystemExit(f"[오류] images 폴더가 없습니다: {img_dir}")
    return [(p, lbl_dir / f"{p.stem}.txt")
            for p in sorted(img_dir.iterdir()) if p.suffix.lower() in IMAGE_EXTS]


class CrowdPersonDataset(Dataset):
    def __init__(self, pairs: list[tuple[Path, Path]], size: int):
        self.pairs = pairs
        self.size = size

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, i: int):
        img_p, lbl_p = self.pairs[i]
        # cv2.imread 대신 imread_unicode — 학습 데이터 보관소 경로가 한글
        # 폴더명을 포함하면 Windows에서 cv2.imread 가 조용히 None 을
        # 반환한다(2026-08-25, 침수 학습에서 실제로 겪은 것과 같은 문제).
        img = imread_unicode(img_p)
        if img is None:
            raise RuntimeError(f"이미지를 읽지 못했습니다: {img_p}")
        h0, w0 = img.shape[:2]
        boxes = yolo_boxes(lbl_p, w0, h0)
        img = cv2.resize(img, (self.size, self.size))
        sx, sy = self.size / w0, self.size / h0
        if len(boxes):
            boxes = boxes * np.array([sx, sy, sx, sy], np.float32)
        x = torch.from_numpy(img[:, :, ::-1].transpose(2, 0, 1).copy()).float() / 255.0
        target = {
            "boxes": torch.from_numpy(boxes).float(),
            "labels": torch.ones((len(boxes),), dtype=torch.int64),  # 1 = person
        }
        return x, target


def _collate(batch):
    images, targets = zip(*batch)
    return list(images), list(targets)


@torch.no_grad()
def evaluate(model, dl, device, conf: float = 0.5, iou_th: float = 0.5) -> tuple[float, float, float]:
    """(precision, recall, f1) — IoU 매칭 기반 자체 계산."""
    model.eval()
    tp = fp = fn = 0
    for images, targets in dl:
        images = [im.to(device) for im in images]
        preds = model(images)
        for pred, gt in zip(preds, targets):
            keep = pred["scores"] >= conf
            pboxes = pred["boxes"][keep].cpu().numpy()
            gboxes = gt["boxes"].numpy()
            matched = set()
            for pb in pboxes:
                best_iou, best_j = 0.0, -1
                for j, gb in enumerate(gboxes):
                    if j in matched:
                        continue
                    iou = _iou(pb, gb)
                    if iou > best_iou:
                        best_iou, best_j = iou, j
                if best_iou >= iou_th and best_j >= 0:
                    matched.add(best_j)
                    tp += 1
                else:
                    fp += 1
            fn += len(gboxes) - len(matched)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, default=640)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    random.seed(0)
    torch.manual_seed(0)

    data_dir = resolve_dataset("crowd_person_yolo")
    if data_dir is None:
        raise SystemExit(
            "[오류] 인파 사람검출 학습 데이터가 없습니다.\n"
            f"       다음 위치에 YOLO 형식으로 채워 넣으십시오: "
            f"{dataset_hint('crowd_person_yolo')}")

    pairs = collect_pairs(data_dir)
    if not pairs:
        raise SystemExit(f"[오류] 이미지가 없습니다: {data_dir}")
    random.shuffle(pairs)
    n_val = max(1, int(len(pairs) * args.val_frac))
    val_pairs, train_pairs = pairs[:n_val], pairs[n_val:]
    print(f"[crowd-train] 총 {len(pairs)}장 -> train {len(train_pairs)} / val {len(val_pairs)}")

    device = torch.device("cpu")
    train_dl = DataLoader(CrowdPersonDataset(train_pairs, args.size), batch_size=args.batch,
                          shuffle=True, num_workers=0, collate_fn=_collate)
    val_dl = DataLoader(CrowdPersonDataset(val_pairs, args.size), batch_size=args.batch,
                        shuffle=False, num_workers=0, collate_fn=_collate)

    from torchvision.models.detection import fasterrcnn_mobilenet_v3_large_fpn

    model = fasterrcnn_mobilenet_v3_large_fpn(weights=None, weights_backbone="DEFAULT",
                                              num_classes=2)  # 배경 + person
    model.to(device)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr)

    run = RUNS / (args.name or f"frcnn_{args.size}")
    run.mkdir(parents=True, exist_ok=True)
    csv = run / "results.csv"
    csv.write_text("epoch,train_loss,precision,recall,f1,minutes\n", encoding="utf-8")

    best = -1.0
    t_start = time.time()
    for ep in range(1, args.epochs + 1):
        model.train()
        tot = nb = 0
        t0 = time.time()
        for images, targets in train_dl:
            images = [im.to(device) for im in images]
            targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
            loss_dict = model(images, targets)
            loss = sum(loss_dict.values())
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss.item())
            nb += 1
        precision, recall, f1 = evaluate(model, val_dl, device)
        mins = (time.time() - t_start) / 60
        line = f"{ep},{tot / max(nb,1):.4f},{precision:.4f},{recall:.4f},{f1:.4f},{mins:.1f}"
        with csv.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"[crowd-train] epoch {ep}/{args.epochs}  loss {tot / max(nb,1):.4f}  "
              f"precision {precision:.4f}  recall {recall:.4f}  F1 {f1:.4f}  "
              f"({(time.time() - t0) / 60:.1f}분/epoch, 누적 {mins:.0f}분)")

        if f1 > best:
            best = f1
            torch.save({"model": model.state_dict(), "arch": "fasterrcnn_mobilenet_v3_large_fpn",
                        "size": args.size, "val_f1": f1, "val_precision": precision,
                        "val_recall": recall, "epoch": ep,
                        "license_note": "torchvision(BSD) 기반 - AGPL 아님"},
                       run / "best.pt")
    print(f"\n[crowd-train] 완료 - 최고 val F1 {best:.4f} -> {run / 'best.pt'}")


if __name__ == "__main__":
    main()
