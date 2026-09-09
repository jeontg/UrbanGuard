"""RDD2022(Pascal VOC XML) -> YOLO 학습 포맷 변환 (Phase 2 프로토타입용).

data/datasets/rdd2022_czech/extracted/Czech/train/{images,annotations/xmls}를
읽어 road/defect_detection.py의 2클래스 체계(pothole/crack)로 매핑한 YOLO
데이터셋을 만든다.

클래스 매핑 (RDD 표준 4클래스 -> 이 프로젝트의 2클래스, docs/road_surface_management_plan.md
3-2절 등급 체계가 포트홀/균열만 구분하므로 단순화):
    D00(종주균열)/D10(횡주균열)/D20(악어등균열) -> 1 crack
    D40(포트홀)                                -> 0 pothole

라벨 없는(무손상) 이미지도 배경(negative) 예시로 포함한다(빈 .txt 생성).
train/val은 파일명 순서 10개 중 1개를 val로 고정 분리(재현 가능하도록 난수 미사용).

Usage:
    python scripts/prepare_rdd2022_yolo.py
"""
from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT

SRC = PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "extracted" / "Czech" / "train"
OUT = PROJECT_ROOT / "data" / "datasets" / "rdd2022_czech" / "yolo"

CLASS_MAP = {"D00": 1, "D10": 1, "D20": 1, "D40": 0}  # -> {0: pothole, 1: crack}
VAL_EVERY_N = 10  # 10장 중 1장을 val로 (고정, 재현 가능)


def convert_one(xml_path: Path) -> tuple[str, list[str]] | None:
    root = ET.parse(xml_path).getroot()
    size = root.find("size")
    if size is None:
        return None
    w = float(size.findtext("width"))
    h = float(size.findtext("height"))
    if w <= 0 or h <= 0:
        return None

    lines = []
    for obj in root.findall("object"):
        name = obj.findtext("name")
        cls_id = CLASS_MAP.get(name)
        if cls_id is None:
            continue  # RDD 표준 4클래스 외 다른 값은 무시(이번 조사에서는 발견 안 됨)
        box = obj.find("bndbox")
        xmin, ymin = float(box.findtext("xmin")), float(box.findtext("ymin"))
        xmax, ymax = float(box.findtext("xmax")), float(box.findtext("ymax"))
        xc = ((xmin + xmax) / 2) / w
        yc = ((ymin + ymax) / 2) / h
        bw = (xmax - xmin) / w
        bh = (ymax - ymin) / h
        lines.append(f"{cls_id} {xc:.6f} {yc:.6f} {bw:.6f} {bh:.6f}")
    return root.findtext("filename"), lines


def main() -> None:
    xml_dir = SRC / "annotations" / "xmls"
    img_dir = SRC / "images"
    xml_files = sorted(xml_dir.glob("*.xml"))
    print(f"[prepare_rdd2022_yolo] found {len(xml_files)} annotation files")

    for split in ("train", "val"):
        (OUT / split / "images").mkdir(parents=True, exist_ok=True)
        (OUT / split / "labels").mkdir(parents=True, exist_ok=True)

    n_boxes = 0
    n_images = 0
    for i, xml_path in enumerate(xml_files):
        result = convert_one(xml_path)
        if result is None:
            continue
        filename, lines = result
        img_path = img_dir / filename
        if not img_path.exists():
            continue

        split = "val" if (i % VAL_EVERY_N == 0) else "train"
        shutil.copy(img_path, OUT / split / "images" / filename)
        label_path = OUT / split / "labels" / (Path(filename).stem + ".txt")
        label_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        n_images += 1
        n_boxes += len(lines)

    data_yaml = OUT / "data.yaml"
    data_yaml.write_text(
        f"train: {(OUT / 'train' / 'images').as_posix()}\n"
        f"val: {(OUT / 'val' / 'images').as_posix()}\n"
        "nc: 2\n"
        "names: ['pothole', 'crack']\n",
        encoding="utf-8",
    )
    print(f"[prepare_rdd2022_yolo] wrote {n_images} images, {n_boxes} boxes total")
    print(f"[prepare_rdd2022_yolo] data.yaml: {data_yaml}")


if __name__ == "__main__":
    main()
