"""인파 학습 데이터 자동 라벨링 — 사전학습 검출기로 사람 상자 초안을 만든다.

## 왜 자동 라벨링인가

TfL JamCams로 받은 지점들(`scripts/collect_tfl_frames.py --domain crowd`)은
사람 상자 라벨이 없다. 사람이 일일이 그리는 대신, 이미 운영에서 검증된
사전학습 검출기 — ``crowd/live_analyzer.py``의 ``DetectorCrowdSource``
(torchvision Faster R-CNN, 2×2 타일 추론 — 데모 장면에서 SAM3 결과와
100% 일치 확인된 방식, 같은 모듈 docstring 참고) — 를 그대로 돌려 초안
라벨을 만든다.

## ⚠️ 이것은 가라벨(pseudo-label)입니다

**사람이 확인한 정답이 아니라 모델이 낸 결과입니다.** 오검출·누락이 섞여
있을 수 있고, 특히 원거리·저해상도(TfL은 352×288 고정 — `docs/202608251307/
open_cctv_by_domain_classification.md` §4-3)에서는 놓치는 사람이 있을 수
있습니다. **실제 학습에 쓰기 전 육안 확인·정제를 권장합니다** — 이
스크립트는 "빈손에서 시작하지 않게" 초안만 만듭니다. 저장되는 각 라벨
파일 옆에 ``<이름>.pseudo`` 마커 파일을 함께 남겨, 나중에 "이게 자동
라벨이었는지"를 구분할 수 있게 한다.

## 입력 / 출력

입력: ``<archive>/raw/tfl_london/<camera_id>/<timestamp>.jpg``
　(``scripts/collect_tfl_frames.py --domain crowd`` 로 수집한 원본)

출력: ``<archive>/crowd_person_own/yolo/{images,labels}/`` — YOLO 상자
　형식(단일 클래스 person, id 0), ``train_crowd_person_frcnn.py`` 가
　그대로 읽는 위치.

Usage:
    python scripts/label_crowd_person_auto.py
    python scripts/label_crowd_person_auto.py --conf 0.3   # 더 민감하게(오검출 늘 수 있음)
"""
from __future__ import annotations

import argparse

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import DATA_ARCHIVE_ROOT
from tot_dashboard.common.video_io import IMAGE_EXTS, imread_unicode

RAW_ROOT = DATA_ARCHIVE_ROOT / "08_학습데이터_인파" / "crowd_person_own" / "raw" / "tfl_london"
OUT_ROOT = DATA_ARCHIVE_ROOT / "08_학습데이터_인파" / "crowd_person_own" / "yolo"


def boxes_to_yolo_lines(boxes, img_w: int, img_h: int) -> list[str]:
    """xyxy 픽셀 좌표 -> YOLO 상자 텍스트 줄(class cx cy w h, 0~1)."""
    lines = []
    for x1, y1, x2, y2 in boxes:
        cx, cy = (x1 + x2) / 2 / img_w, (y1 + y2) / 2 / img_h
        bw, bh = (x2 - x1) / img_w, (y2 - y1) / img_h
        lines.append(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", type=float, default=0.4,
                    help="검출 신뢰도 임계값(기본 0.4 — DetectorCrowdSource 기본값과 동일)")
    args = ap.parse_args()

    if not RAW_ROOT.is_dir():
        raise SystemExit(f"[label_crowd_person_auto] 원본 폴더가 없습니다: {RAW_ROOT}")

    print("[label_crowd_person_auto] 검출기 로딩 중(torchvision Faster R-CNN)...")
    from tot_dashboard.crowd.live_analyzer import DetectorCrowdSource
    detector = DetectorCrowdSource(conf=args.conf)

    img_dir, lbl_dir = OUT_ROOT / "images", OUT_ROOT / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    cam_dirs = sorted(d for d in RAW_ROOT.iterdir() if d.is_dir())
    print(f"[label_crowd_person_auto] 지점 {len(cam_dirs)}개 처리")

    total_boxes, empty_count = 0, 0
    for cam_dir in cam_dirs:
        imgs = sorted(p for p in cam_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
        if not imgs:
            continue
        src = imgs[0]  # 지점당 지금은 1장뿐(추후 여러 시각을 모으면 전부 처리하도록 바꿀 것)
        frame = imread_unicode(src)
        if frame is None:
            print(f"[label_crowd_person_auto]   ⚠ 이미지를 읽지 못함: {src}")
            continue
        h, w = frame.shape[:2]
        boxes, scores = detector.boxes_from_frame(frame)

        stem = cam_dir.name
        out_img = img_dir / f"{stem}.jpg"
        out_img.write_bytes(src.read_bytes())
        lines = boxes_to_yolo_lines(boxes, w, h)
        (lbl_dir / f"{stem}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        # 가라벨 표시 — 사람이 검수한 라벨과 구분하기 위한 마커.
        (lbl_dir / f"{stem}.pseudo").write_text(
            f"conf>={args.conf}, model=fasterrcnn_mobilenet_v3_large_fpn(COCO), "
            f"detections={len(boxes)}\n", encoding="utf-8")

        total_boxes += len(boxes)
        if len(boxes) == 0:
            empty_count += 1
        print(f"[label_crowd_person_auto]   {stem}: 검출 {len(boxes)}명 "
              f"(신뢰도 {', '.join(f'{s:.2f}' for s in scores[:5])}{'...' if len(scores) > 5 else ''})")

    print(f"\n[label_crowd_person_auto] 완료 — {len(cam_dirs)}개 지점, "
          f"전체 검출 {total_boxes}건, 사람 0명인 지점 {empty_count}개")
    print(f"[label_crowd_person_auto] 저장: {OUT_ROOT}")
    print("[label_crowd_person_auto] ⚠ 가라벨(pseudo-label)입니다 — 실제 학습 전 "
          "육안 확인을 권장합니다. .pseudo 마커 파일로 자동 라벨임을 표시해 뒀습니다.")


if __name__ == "__main__":
    main()
