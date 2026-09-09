"""침수 세그멘테이션 마스크(PNG) → YOLO-seg 폴리곤 라벨 변환.

HydroShare "Urban Flood Image Dataset"(CC BY 4.0)처럼 ``image/`` + ``mask/``
구조로 배포되는 데이터셋을, 우리 파이프라인의 표준 포맷인 **YOLO-seg 폴리곤**
으로 변환한다(``scripts/label_flood_water.py``가 저장하는 것과 동일한 포맷).

이 포맷을 표준으로 삼는 이유는 Ultralytics와 RF-DETR-Seg가 **둘 다 그대로 읽기**
때문이다 — 프레임워크를 나중에 바꿔도 데이터를 다시 만들 필요가 없다
(docs/flood_water_dataset_workflow.md 참고).

마스크 인코딩은 자동 판별한다(0/1 이든 0/255 이든 **0 초과를 물로 간주**).
HydroShare 데이터셋은 0/1 인코딩이라 흔히 쓰는 ``>127`` 임계값으로는 전부
비어 보이므로 주의가 필요하다.

출력: data/datasets/flood_water_own/labeled/<set>/{images,labels}/
      (자체 라벨링 결과와 같은 위치에 쌓여 그대로 합쳐서 학습 가능)

Usage:
    # HydroShare 3종 일괄 변환
    python scripts/convert_flood_masks_to_yolo.py --preset hydroshare

    # 임의 데이터셋
    python scripts/convert_flood_masks_to_yolo.py \
        --images <이미지폴더> --masks <마스크폴더> --set-name my_set
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import IMAGE_EXTS

CLASS_ID = 0  # flood_water
OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "labeled"
HYDRO_ROOT = PROJECT_ROOT / "data" / "datasets" / "urban_flood_hydroshare"

# (세트명, 이미지 폴더, 마스크 폴더, 마스크 파일명 규칙)
#   Sazara만 image_N.jpg -> label_N.png 로 접두어가 다르다.
HYDROSHARE_SETS = [
    ("hydroshare_deepflood", HYDRO_ROOT / "Deepflood/image", HYDRO_ROOT / "Deepflood/mask", "same"),
    ("hydroshare_sazara", HYDRO_ROOT / "Sazara/image", HYDRO_ROOT / "Sazara/mask", "sazara"),
    ("hydroshare_webcoos", HYDRO_ROOT / "WebCOOS/image", HYDRO_ROOT / "WebCOOS/mask", "same"),
]


def _mask_path(mask_dir: Path, stem: str, rule: str) -> Path:
    if rule == "sazara":                      # image_12 -> label_12
        return mask_dir / f"label_{stem.split('_')[-1]}.png"
    return mask_dir / f"{stem}.png"


def mask_to_polygons(mask: np.ndarray, min_area_frac: float = 0.001,
                     epsilon_frac: float = 0.002) -> list[np.ndarray]:
    """이진 마스크 -> 단순화된 외곽 폴리곤 목록.

    - ``min_area_frac``: 전체 화면 대비 이 비율 미만인 조각은 노이즈로 버린다
    - ``epsilon_frac``: 둘레 대비 근사 허용오차. 점 수를 줄여 라벨 파일을 가볍게 한다
    - 구멍(hole)은 표현하지 않는다. YOLO-seg 포맷 자체가 단일 외곽선만 지원하며,
      침수 면적 산출 목적에는 충분하다.
    """
    h, w = mask.shape[:2]
    binm = (mask > 0).astype(np.uint8)        # 0/1, 0/255 모두 대응
    contours, _ = cv2.findContours(binm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        if cv2.contourArea(c) < min_area_frac * h * w:
            continue
        approx = cv2.approxPolyDP(c, epsilon_frac * cv2.arcLength(c, True), True)
        if len(approx) >= 3:
            out.append(approx.reshape(-1, 2))
    return out


def _to_line(poly: np.ndarray, w: int, h: int) -> str:
    coords = []
    for x, y in poly:
        coords.append(f"{min(max(float(x) / w, 0.0), 1.0):.6f}")
        coords.append(f"{min(max(float(y) / h, 0.0), 1.0):.6f}")
    return f"{CLASS_ID} " + " ".join(coords)


def convert(set_name: str, img_dir: Path, mask_dir: Path, rule: str = "same") -> dict:
    if not img_dir.is_dir() or not mask_dir.is_dir():
        print(f"[convert] 건너뜀 (폴더 없음): {img_dir} / {mask_dir}")
        return {"set": set_name, "converted": 0, "skipped": 0, "empty": 0}

    out_img = OUT_ROOT / set_name / "images"
    out_lbl = OUT_ROOT / set_name / "labels"
    out_img.mkdir(parents=True, exist_ok=True)
    out_lbl.mkdir(parents=True, exist_ok=True)

    images = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    n_ok = n_skip = n_empty = 0
    for ip in images:
        mp = _mask_path(mask_dir, ip.stem, rule)
        if not mp.exists():
            n_skip += 1
            continue
        img = cv2.imread(str(ip))
        msk = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
        if img is None or msk is None:
            n_skip += 1
            continue
        h, w = img.shape[:2]
        if msk.shape[:2] != (h, w):           # 해상도 불일치 시 이미지 기준으로 맞춤
            msk = cv2.resize(msk, (w, h), interpolation=cv2.INTER_NEAREST)

        polys = mask_to_polygons(msk)
        lines = [_to_line(p, w, h) for p in polys]
        if not lines:
            n_empty += 1                      # 물 없는 배경 샘플로 유지(빈 라벨)
        (out_lbl / f"{ip.stem}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        dst = out_img / ip.name
        if not dst.exists():
            cv2.imwrite(str(dst), img)
        n_ok += 1

    print(f"[convert] {set_name}: 변환 {n_ok}장 (마스크없음 {n_skip}, 폴리곤0 {n_empty}) -> {OUT_ROOT / set_name}")
    return {"set": set_name, "converted": n_ok, "skipped": n_skip, "empty": n_empty}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=["hydroshare"], default=None,
                    help="사전 정의된 데이터셋 일괄 변환")
    ap.add_argument("--images", default=None, help="이미지 폴더")
    ap.add_argument("--masks", default=None, help="마스크 폴더")
    ap.add_argument("--set-name", default=None, help="출력 세트 이름")
    args = ap.parse_args()

    if args.preset == "hydroshare":
        stats = [convert(n, i, m, r) for n, i, m, r in HYDROSHARE_SETS]
    elif args.images and args.masks:
        name = args.set_name or Path(args.images).parent.name
        stats = [convert(name, Path(args.images), Path(args.masks), "same")]
    else:
        raise SystemExit("--preset hydroshare 또는 --images/--masks 조합이 필요합니다")

    total = sum(s["converted"] for s in stats)
    print(f"\n[convert] 총 {total}장 변환 완료 -> {OUT_ROOT}")


if __name__ == "__main__":
    main()
