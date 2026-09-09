"""낙하물·화재연기 영상에서 라벨링용 프레임을 추출한다 (Phase 7).

확보한 사고·화재 영상(뉴스 영상, 지자체 제공 자료, 자체 촬영분 등 — 저작권
확인 필요, 아래 참고)에서 일정 간격으로 프레임을 뽑아
``scripts/label_traffic_incident.py``의 입력으로 넘긴다.
``scripts/extract_flood_frames.py``와 같은 패턴이다(레터박스 자동 제거 포함).

⚠️ **영상 저작권을 먼저 확인하세요.** 뉴스 영상·SNS 영상은 함부로 학습에
쓸 수 없다 — ``docs/flood_water_dataset_workflow.md``의 Mendeley 데이터셋
사례("CC BY 4.0은 포장에만 적용, 원저작물 권리는 별도")를 참고. 공개
데이터셋을 쓸 경우의 라이선스 검토는 이번 회차 조사 문서를 먼저 보세요
(``docs/<생성일시>/traffic_incident_dataset_license_investigation.md``).

출력: data/datasets/traffic_incident_own/raw/<set_name>/<영상명>_<프레임번호>.jpg

Usage:
    python scripts/extract_traffic_incident_frames.py --video <경로>
    python scripts/extract_traffic_incident_frames.py --video <경로> --every 15 --set-name news_2026fire
    python scripts/extract_traffic_incident_frames.py --dir <영상폴더> --every 30
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import (VIDEO_EXTS, ascii_stem,
                                           detect_letterbox, imwrite_unicode)

OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "traffic_incident_own" / "raw"


def extract_one(video: Path, out_dir: Path, every: int, crop: bool) -> int:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"[extract_traffic_incident_frames] 열 수 없음: {video}")
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
                        print(f"[extract_traffic_incident_frames]   레터박스 감지 -> crop "
                              f"top={t} bottom={frame.shape[0] - b} "
                              f"left={l} right={frame.shape[1] - r}")
                t, b, l, r = box
                frame = frame[t:b, l:r]
            out_path = out_dir / f"{ascii_stem(video.stem)}_{idx:06d}.jpg"
            if imwrite_unicode(out_path, frame):
                saved += 1
            elif saved == 0 and idx == 0:
                print(f"[extract_traffic_incident_frames]   저장 실패: {out_path}")
        idx += 1
    cap.release()
    print(f"[extract_traffic_incident_frames] {video.name}: {idx}프레임 중 {saved}장 저장")
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
    print(f"\n[extract_traffic_incident_frames] 총 {total}장 -> {out_dir}")
    print(f"[extract_traffic_incident_frames] 다음: python scripts/label_traffic_incident.py --folder {out_dir}")


if __name__ == "__main__":
    main()
