"""배경(네거티브) 샘플을 학습셋에 추가 — 물이 없는 이미지 + 빈 라벨.

## 왜 필요한가

공개 침수 데이터셋(HydroShare 등)은 **전부 침수 장면**이라 물 비율이 평균 43~51%다.
배경 샘플이 하나도 없는 상태로 학습하면 모델이 "도로 = 물"로 일반화해버려,
맑은 날 마른 아스팔트를 물로 칠하는 심각한 오탐이 발생한다(실측 확인: 맑은 날
부산 CCTV 14장 평균 28%, 최대 65% 오탐).

이 스크립트는 물이 없는 이미지를 **빈 라벨(.txt 0바이트)** 과 함께 학습셋에 넣어
모델이 "물이 없는 장면"도 배우게 한다.

## 특히 유용한 하드 네거티브

- **폭우 장면이지만 도로는 잠기지 않은 영상** — "비 ≠ 침수"를 가르친다
- **맑은 날 실제 운영 CCTV 프레임** — 배포 환경 그대로라 가장 효과가 크다
- 젖은 노면(반사광)은 있으나 고인 물은 없는 장면

Usage:
    # 폴더의 모든 이미지를 배경 샘플로 추가
    python scripts/add_negative_samples.py --folder <이미지폴더> --set-name neg_okcheon

    # 부산 CCTV 수집분 전체를 배경 샘플로
    python scripts/add_negative_samples.py --busan-cctv --set-name neg_busan_dry
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import IMAGE_EXTS

LABELED_ROOT = PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "labeled"
BUSAN_RAW = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "raw"


def add_negatives(images: list[Path], set_name: str) -> int:
    out_img = LABELED_ROOT / set_name / "images"
    out_lbl = LABELED_ROOT / set_name / "labels"
    out_img.mkdir(parents=True, exist_ok=True)
    out_lbl.mkdir(parents=True, exist_ok=True)

    n = 0
    for src in images:
        # 파일명 충돌 방지: 상위 폴더명을 접두어로
        dst_name = f"{src.parent.name}_{src.name}"
        dst = out_img / dst_name
        if not dst.exists():
            shutil.copy(src, dst)
        # 빈 라벨 = 물 없음(배경)
        (out_lbl / f"{Path(dst_name).stem}.txt").write_text("", encoding="utf-8")
        n += 1
    print(f"[negatives] {set_name}: {n}장 추가 -> {LABELED_ROOT / set_name}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default=None, help="배경 샘플로 쓸 이미지 폴더")
    ap.add_argument("--busan-cctv", action="store_true",
                    help="data/datasets/road_cctv_own/raw/ 전체를 배경 샘플로 추가")
    ap.add_argument("--set-name", default=None, help="출력 세트 이름")
    ap.add_argument("--limit", type=int, default=None, help="최대 장수 제한")
    args = ap.parse_args()

    if args.busan_cctv:
        images = sorted(p for p in BUSAN_RAW.rglob("*") if p.suffix.lower() in IMAGE_EXTS)
        set_name = args.set_name or "neg_busan_dry"
    elif args.folder:
        d = Path(args.folder)
        if not d.is_dir():
            raise SystemExit(f"폴더가 없습니다: {d}")
        images = sorted(p for p in d.rglob("*") if p.suffix.lower() in IMAGE_EXTS)
        set_name = args.set_name or f"neg_{d.name}"
    else:
        raise SystemExit("--folder 또는 --busan-cctv 중 하나가 필요합니다")

    if not images:
        raise SystemExit("이미지를 찾지 못했습니다")
    if args.limit:
        images = images[:args.limit]

    add_negatives(images, set_name)
    print("[negatives] 다음: python scripts/train_flood_water_cpu.py --epochs 40")


if __name__ == "__main__":
    main()
