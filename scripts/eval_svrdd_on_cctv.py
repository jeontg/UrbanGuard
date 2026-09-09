"""학습한 모델이 **부산 CCTV 프레임에서 실제로 잡는가**를 가른다.

Phase 2 의 검증 방법을 그대로 쓴다. 검증셋 성능만 보면 안 되는 이유가 거기
있다 — RDD2022 로 학습한 v2 는 **검증셋 mAP50 이 0.23 까지 올랐는데도** 부산
CCTV 에서는 conf 를 0.01 까지 낮춰도 0건이었다. 즉 자기 도메인 성적은 배포
환경에서의 쓸모를 전혀 보장하지 않는다.

그래서 항상 **두 곳에 같이 돌린다.**

자기 도메인(SVRDD val)
    모델이 애초에 학습에 실패한 것인지 가른다. 여기서도 0건이면 학습이 잘못된
    것이고, 여기서만 잡히면 도메인 갭이다.

부산 CCTV(실제 수집 프레임)
    우리가 배포할 환경이다. ``data/datasets/road_cctv_own/raw`` 에 상시 수집·
    업로드로 모인 프레임을 쓴다.

신뢰도를 여러 단계로 낮춰 보는 이유
    0.25 에서 0건인 것과 0.01 에서도 0건인 것은 뜻이 다르다. 앞은 「자신이
    없다」이고 뒤는 「아무 신호도 없다」이다. 후자면 더 학습해도 달라지지 않는다.

Usage:
    python scripts/eval_svrdd_on_cctv.py --model <best.pt>
    python scripts/eval_svrdd_on_cctv.py --model <best.pt> --save-samples 12
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from tot_dashboard.common.config import PROJECT_ROOT

CCTV_DIR = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "raw"
SVRDD_VAL = PROJECT_ROOT / "data" / "datasets" / "svrdd" / "yolo" / "val" / "images"
OUT_DIR = PROJECT_ROOT / "data" / "datasets" / "svrdd" / "eval"
CONFS = (0.25, 0.10, 0.01)
NAMES = {0: "pothole", 1: "crack"}


def imread(path: Path):
    """한글 경로에서도 읽는다 — OpenCV 는 Windows 에서 조용히 None 을 준다."""
    try:
        return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None


def imwrite(path: Path, img) -> bool:
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        return False
    try:
        path.write_bytes(buf.tobytes())
        return True
    except OSError:
        return False


def collect(root: Path, limit: int | None = None) -> list[Path]:
    if not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*.jpg"))
    return files[:limit] if limit else files


def run(model, files: list[Path], conf: float) -> tuple[int, Counter, list]:
    """이미지 목록에 모델을 돌려 탐지 수를 센다."""
    hits, per_class, sample = 0, Counter(), []
    for p in files:
        img = imread(p)
        if img is None:
            continue
        res = model.predict(img, conf=conf, verbose=False)[0]
        boxes = getattr(res, "boxes", None)
        n = 0 if boxes is None else len(boxes)
        if n:
            hits += n
            for c in boxes.cls.tolist():
                per_class[NAMES.get(int(c), str(int(c)))] += 1
            sample.append((p, res))
    return hits, per_class, sample


def report(title: str, files: list[Path], model, save: int, tag: str) -> dict:
    print(f"\n── {title} — {len(files)}장 " + "─" * 30)
    if not files:
        print("   대상 이미지가 없습니다.")
        return {}
    out = {}
    for conf in CONFS:
        hits, per_class, sample = run(model, files, conf)
        detail = (" · ".join(f"{k} {v}" for k, v in sorted(per_class.items()))
                  or "없음")
        imgs = len({p for p, _ in sample})
        print(f"   conf {conf:>4} → 탐지 {hits:4d}건 / {imgs:3d}장에서 · {detail}")
        out[conf] = hits
        # 가장 낮은 임계에서만 표본을 남긴다 — 무엇을 보고 반응했는지 확인용.
        if save and conf == CONFS[-1] and sample:
            d = OUT_DIR / tag
            d.mkdir(parents=True, exist_ok=True)
            for p, res in sample[:save]:
                imwrite(d / f"{p.parent.name}_{p.stem}.jpg", res.plot())
            print(f"   표본 저장: {d}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="SVRDD 학습 모델의 부산 CCTV 검증")
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--limit-val", type=int, default=200,
                    help="자기 도메인 검증에 쓸 장수(전량은 오래 걸린다)")
    ap.add_argument("--save-samples", type=int, default=8)
    args = ap.parse_args()

    if not args.model.is_file():
        print(f"[오류] 체크포인트가 없습니다: {args.model}")
        return

    from ultralytics import YOLO
    model = YOLO(str(args.model))
    print(f"[eval] 모델 {args.model}")

    own = report("자기 도메인 (SVRDD val)", collect(SVRDD_VAL, args.limit_val),
                 model, args.save_samples, "svrdd_val")
    cctv = report("배포 환경 (부산 CCTV 수집 프레임)", collect(CCTV_DIR),
                  model, args.save_samples, "busan_cctv")

    # ── 판정 ────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    own_low = own.get(CONFS[-1], 0)
    cctv_low = cctv.get(CONFS[-1], 0)
    if not own and not cctv:
        print("판정 불가 — 양쪽 모두 대상 이미지가 없습니다.")
    elif own_low == 0:
        print("✗ 자기 도메인에서도 0건입니다 — **학습 자체가 실패**했습니다.")
        print("  데이터 변환(클래스 매핑)이나 학습 설정을 먼저 확인하세요.")
    elif cctv_low == 0:
        print("✗ 자기 도메인은 잡는데 부산 CCTV 는 0건 — **도메인 갭이 남아**")
        print("  있습니다. RDD2022 때와 같은 결과이며, 45° 부감으로도 관제")
        print("  CCTV 해상도(320~352×240)는 넘지 못한다는 뜻입니다.")
        print("  → 더 학습해도 달라지지 않을 가능성이 높습니다.")
        print("    노면 전용 촬영(차량 탑재·저각 설치) 도입 검토가 필요합니다.")
    else:
        print(f"△ 부산 CCTV 에서 탐지가 나왔습니다 (conf 0.25 기준 "
              f"{cctv.get(CONFS[0], 0)}건).")
        print("  ⚠️ **이것만으로 「된다」고 판단하면 안 됩니다.** 탐지 수는")
        print("     신호의 증거가 아닙니다 — 무엇을 보고 반응했는지가 전부입니다.")
        print(f"     표본을 반드시 눈으로 확인하세요: {OUT_DIR / 'busan_cctv'}")
        print()
        print("  특히 이 두 가지가 흔한 오탐입니다.")
        print("   · **차선 도색** — 균열과 똑같이 가늘고 긴 선이라 가장 많이 헷갈립니다")
        print("   · **도로 이음매·그림자** — 저해상도에서 균열과 구분되지 않습니다")
        print("  실제로 2026-08-12 시험에서 부산 CCTV 탐지 전부가 차선 도색이었습니다.")
    print("=" * 60)
    print("\n⚠ 탐지 0건이 「손상 없음」을 뜻하지 않습니다. 모델이 못 본 것과")
    print("  실제로 없는 것은 다릅니다.")
    print("⚠ 반대로 탐지가 있다고 「된다」는 뜻도 아닙니다. 엉뚱한 것을 잡는 모델은")
    print("  아무것도 못 잡는 모델보다 운영에서 더 나쁩니다 — 신뢰를 잃습니다.")


if __name__ == "__main__":
    main()
