# -*- coding: utf-8 -*-
"""교통 차량검출 학습 데이터 자동 라벨링 — 사전학습 YOLO11s로 차량 상자
초안을 만든다.

2026-08-29, 「4대탐지기능 성능개선 로드맵」 4단계
(``scripts/collect_traffic_night_rain_frames.py``의 다음 단계).

## 왜 자동 라벨링인가

``crowd/label_crowd_person_auto.py``(2026-08-26)와 **의도적으로 같은
패턴**이다 — 이미 운영에서 쓰는 사전학습 검출기(YOLO11s, COCO)를 그대로
돌려 초안 라벨을 만든다. 야간·우천 조건은 이 사전학습 모델이 원래도
자신 없는 조건이라(오탐·미탐 증가가 예상되는 이유이기도 하다), 초안의
품질이 주간보다 낮을 수 있다 — 그래서 더더욱 사람 확인이 필요하다.

## ⚠️ 이것은 가라벨(pseudo-label)입니다

**사람이 확인한 정답이 아니라 모델이 낸 결과입니다.** 특히 이 데이터셋의
목적 자체가 "모델이 잘 못 보는 조건을 보강"하는 것이라, 가라벨의 누락이
평소보다 많을 수 있습니다. **실제 학습에 쓰기 전 육안 확인·정제를
반드시 권장합니다.** 저장되는 각 라벨 파일 옆에 ``.pseudo`` 마커 파일을
남긴다(``label_crowd_person_auto.py``와 같은 표시 방식).

## 입력 / 출력

입력: ``<보관소>/07_학습데이터_교통/traffic_vehicle_own/raw/night_rain/
<카메라ID>/<timestamp>_<조건>.jpg``
(``scripts/collect_traffic_night_rain_frames.py`` 로 수집한 원본)

출력: ``<보관소>/07_학습데이터_교통/traffic_vehicle_own/yolo/
{images,labels}/`` + ``data.yaml`` — ``scripts/train_traffic_vehicle_
yolo.py``가 그대로 읽는 위치·형식. 클래스 순서는 이 스크립트가 확정한다
(``CLASS_NAMES``) — 라벨 파일의 클래스 번호가 이 순서와 어긋나면 학습이
엉뚱한 것을 배운다.

Usage:
    python scripts/label_traffic_vehicle_auto.py
    python scripts/label_traffic_vehicle_auto.py --conf 0.25   # 더 민감하게(오검출 늘 수 있음)
"""
from __future__ import annotations

import argparse

from tot_dashboard.common.data_archive import DATA_ARCHIVE_ROOT
from tot_dashboard.common.video_io import IMAGE_EXTS, imread_unicode

RAW_ROOT = (DATA_ARCHIVE_ROOT / "07_학습데이터_교통"
           / "traffic_vehicle_own" / "raw" / "night_rain")
OUT_ROOT = DATA_ARCHIVE_ROOT / "07_학습데이터_교통" / "traffic_vehicle_own" / "yolo"

# ⚠️ train_traffic_vehicle_yolo.py 문서의 클래스 목록과 순서를 그대로
# 고정한다 — 순서를 바꾸면 기존에 이미 라벨링해 둔 파일의 클래스 번호가
# 전부 어긋난다.
CLASS_NAMES = ["car", "bus", "truck", "motorcycle"]


def boxes_to_yolo_lines(boxes, class_ids, img_w: int, img_h: int) -> list[str]:
    """xyxy 픽셀 좌표 + 클래스 id -> YOLO 상자 텍스트 줄(class cx cy w h, 0~1)."""
    lines = []
    for (x1, y1, x2, y2), cid in zip(boxes, class_ids):
        cx, cy = (x1 + x2) / 2 / img_w, (y1 + y2) / 2 / img_h
        bw, bh = (x2 - x1) / img_w, (y2 - y1) / img_h
        lines.append(f"{cid} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    return lines


def _write_data_yaml() -> None:
    """train_traffic_vehicle_yolo.py가 읽는 data.yaml. 이미 있으면 손대지
    않는다 — 사람이 손으로 고쳤을 수 있다(예: 검증셋 분리 후 val 경로 추가)."""
    path = OUT_ROOT / "data.yaml"
    if path.is_file():
        return
    names_block = "\n".join(f"  {i}: {n}" for i, n in enumerate(CLASS_NAMES))
    path.write_text(
        f"path: {OUT_ROOT.as_posix()}\n"
        "train: images\n"
        "val: images\n"   # ⚠️ 검증셋 미분리 — 데이터가 쌓이면 별도로 나눌 것
        f"names:\n{names_block}\n",
        encoding="utf-8")
    print(f"[label_traffic_vehicle_auto] data.yaml 생성: {path}\n"
         "  ⚠ train/val이 같은 폴더를 가리킵니다 — 데이터가 쌓이면 "
         "검증셋을 별도로 분리하세요(과적합 여부를 알 수 없게 됩니다).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", type=float, default=0.3,
                    help="검출 신뢰도 임계값(기본 0.3 — 야간·우천은 신뢰도가 "
                        "낮게 나오는 경향이 있어 주간 기본값 0.4보다 낮춤)")
    args = ap.parse_args()

    if not RAW_ROOT.is_dir():
        raise SystemExit(
            f"[label_traffic_vehicle_auto] 원본 폴더가 없습니다: {RAW_ROOT}\n"
            "       scripts/collect_traffic_night_rain_frames.py 를 먼저 "
            "(반복) 실행해 실제 야간·강수 프레임을 모으세요.")

    print("[label_traffic_vehicle_auto] 검출기 로딩 중(YOLO11s, COCO 사전학습)...")
    from ultralytics import YOLO

    from tot_dashboard.common.models_loader import select_class_ids
    from tot_dashboard.traffic_weather.perception.detection_source import VEHICLE_CLASSES

    model = YOLO("models/yolo11s.pt")
    coco_ids = select_class_ids(model, sorted(VEHICLE_CLASSES))
    coco_id_to_name = {cid: model.names[cid] for cid in coco_ids}
    name_to_class_idx = {n: i for i, n in enumerate(CLASS_NAMES)}

    img_dir, lbl_dir = OUT_ROOT / "images", OUT_ROOT / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    cam_dirs = sorted(d for d in RAW_ROOT.iterdir() if d.is_dir())
    print(f"[label_traffic_vehicle_auto] 카메라 {len(cam_dirs)}개 처리")

    total_boxes, empty_count, processed = 0, 0, 0
    for cam_dir in cam_dirs:
        imgs = sorted(p for p in cam_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
        for src in imgs:
            frame = imread_unicode(src)
            if frame is None:
                print(f"[label_traffic_vehicle_auto]   ⚠ 이미지를 읽지 못함: {src}")
                continue
            h, w = frame.shape[:2]
            res = model(frame, conf=args.conf, imgsz=416, classes=coco_ids,
                       verbose=False)[0]

            boxes, class_ids = [], []
            for b in res.boxes:
                cid_coco = int(b.cls[0])
                name = coco_id_to_name.get(cid_coco)
                if name not in name_to_class_idx:
                    continue
                x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
                boxes.append((x1, y1, x2, y2))
                class_ids.append(name_to_class_idx[name])

            stem = f"{cam_dir.name}_{src.stem}"
            out_img = img_dir / f"{stem}.jpg"
            out_img.write_bytes(src.read_bytes())
            lines = boxes_to_yolo_lines(boxes, class_ids, w, h)
            (lbl_dir / f"{stem}.txt").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            (lbl_dir / f"{stem}.pseudo").write_text(
                f"conf>={args.conf}, model=yolo11s.pt(COCO), "
                f"detections={len(boxes)}\n", encoding="utf-8")

            total_boxes += len(boxes)
            processed += 1
            if len(boxes) == 0:
                empty_count += 1
            print(f"[label_traffic_vehicle_auto]   {stem}: 검출 {len(boxes)}대")

    _write_data_yaml()
    print(f"\n[label_traffic_vehicle_auto] 완료 — {processed}장 처리, "
         f"전체 검출 {total_boxes}건, 차량 0대인 장면 {empty_count}개")
    print(f"[label_traffic_vehicle_auto] 저장: {OUT_ROOT}")
    print("[label_traffic_vehicle_auto] ⚠ 가라벨(pseudo-label)입니다 — 실제 학습 전 "
         "육안 확인을 반드시 권장합니다. .pseudo 마커 파일로 자동 라벨임을 표시해 뒀습니다.")


if __name__ == "__main__":
    main()
