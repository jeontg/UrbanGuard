"""침수 영상에서 라벨링용 프레임을 추출한다 (방안 B - 자체 학습 데이터 구축).

확보한 침수 영상(자체 녹화분, 과거 사고 영상, 지자체 제공 자료 등)에서 일정
간격으로 프레임을 뽑아 ``scripts/label_flood_water.py``의 입력으로 넘긴다.

**레터박스 자동 제거**: 검증 중 발견한 것처럼, 좌우/상하에 검은 여백이 있는
영상을 그대로 학습에 쓰면 모델이 검은 띠를 물로 오인한다
(docs/yolo_license_alternatives.md 2-B절 "발견한 버그"). 기본적으로 여백을
잘라내며, ``--no-crop``으로 끌 수 있다.

출력: data/datasets/flood_water_own/raw/<set_name>/<영상명>_<프레임번호>.jpg

Usage:
    python scripts/extract_flood_frames.py --video data/samples/flood/underpath_flood1.mp4
    python scripts/extract_flood_frames.py --video <경로> --every 15 --set-name busan_2026rain
    python scripts/extract_flood_frames.py --dir <영상폴더> --every 30
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import (VIDEO_EXTS, ascii_stem,
                                           detect_letterbox, imwrite_unicode)

OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "raw"

# ascii_stem/imwrite_unicode는 세 번째 사본(extract_traffic_incident_frames.py)이
# 생기며 common/video_io.py로 옮겼다(2026-08-26). detect_letterbox도 같은
# 이유로 이미 그쪽에 있다.


def extract_one(video: Path, out_dir: Path, every: int, crop: bool) -> int:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"[extract_flood_frames] 열 수 없음: {video}")
        return 0
    box = None
    saved = idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % every == 0:
            if crop:
                if box is None:
                    box = detect_letterbox(frame)
                    t, b, l, r = box
                    if (t, b, l, r) != (0, frame.shape[0], 0, frame.shape[1]):
                        print(f"[extract_flood_frames]   레터박스 감지 -> crop "
                              f"top={t} bottom={frame.shape[0] - b} "
                              f"left={l} right={frame.shape[1] - r}")
                t, b, l, r = box
                frame = frame[t:b, l:r]
            out_path = out_dir / f"{ascii_stem(video.stem)}_{idx:06d}.jpg"
            if imwrite_unicode(out_path, frame):
                saved += 1
            elif saved == 0 and idx == 0:
                print(f"[extract_flood_frames]   저장 실패: {out_path}")
        idx += 1
    cap.release()
    print(f"[extract_flood_frames] {video.name}: {idx}프레임 중 {saved}장 저장")
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", default=None, help="영상 파일 1개")
    ap.add_argument("--dir", default=None, help="영상이 들어있는 폴더(하위 전체)")
    ap.add_argument("--every", type=int, default=10,
                    help="N프레임마다 1장 추출 (기본 10). 정적인 장면이면 값을 키우세요")
    ap.add_argument("--set-name", default=None, help="출력 세트 이름 (기본: video/dir 이름)")
    ap.add_argument("--no-crop", action="store_true", help="레터박스 자동 제거 끄기")
    args = ap.parse_args()

    if args.video:
        videos = [Path(args.video)]
        default_set = Path(args.video).stem
    elif args.dir:
        d = Path(args.dir)
        videos = sorted(p for p in d.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
        default_set = d.name
        if not videos:
            raise SystemExit(f"영상이 없습니다: {d}")
    else:
        raise SystemExit("--video 또는 --dir 중 하나가 필요합니다")

    out_dir = OUT_ROOT / (args.set_name or default_set)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = sum(extract_one(v, out_dir, max(1, args.every), not args.no_crop) for v in videos)
    print(f"\n[extract_flood_frames] 총 {total}장 -> {out_dir}")
    print(f"[extract_flood_frames] 다음: python scripts/label_flood_water.py --folder {out_dir}")


if __name__ == "__main__":
    main()
