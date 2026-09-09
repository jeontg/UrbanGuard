"""configs/roi/*.json (레거시 파일 기반 침수 ROI) 을 DB(camera_rois)로 옮긴다.

## 왜 필요한가 (2026-08-22 전수점검)

웹 ROI 편집기(S-81)는 DB에만 저장하는데, 침수 상시 탐지 파이프라인
(``service/runner.py``)은 그동안 ``configs/roi/*.json`` 파일만 읽었다.
``common/roi.load_roi_for_camera()``(DB 우선, 파일 폴백)로 그 배선을
고쳤으니, **이 파일에만 있고 DB에는 없는 ROI를 먼저 옮겨야** 배포 순간
그 카메라들이 ROI 없이(=필터 없이) 도는 공백이 생기지 않는다.

## 안전 규칙

- **DB에 이미 그 카메라·flood 도메인 ROI 행이 있으면 절대 덮어쓰지 않는다**
  — 운영자가 웹에서 이미 다시 그린 값을 파일의 옛 값으로 되돌리면 안 된다.
- 카메라 자체가 DB에 없으면(등록되지 않은 카메라) 건너뛴다 — 만들지 않는다.
- **멱등**이다 — 여러 번 실행해도 이미 옮긴 것은 다시 건드리지 않는다.

## 사용법

    .venv\\Scripts\\python.exe scripts\\backfill_roi_files_to_db.py            # 실제로 반영
    .venv\\Scripts\\python.exe scripts\\backfill_roi_files_to_db.py --dry-run  # 무엇을 할지만 확인

반드시 이 ROI DB 정본화 코드를 배포하기 **전에** 한 번 실행할 것 —
`docs/pending_tasks.md`·배포 런북 참고.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tot_dashboard.common.config import PROJECT_ROOT  # noqa: E402
from tot_dashboard.common.roi import load_roi_config  # noqa: E402
from tot_dashboard.core import cameras as C  # noqa: E402
from tot_dashboard.core.db import get_session  # noqa: E402
from tot_dashboard.core.roles import Domain  # noqa: E402

ROI_DIR = PROJECT_ROOT / "configs" / "roi"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="DB를 바꾸지 않고 무엇을 할지만 출력합니다.")
    args = ap.parse_args(argv)

    if not ROI_DIR.is_dir():
        print(f"[backfill] {ROI_DIR} 폴더가 없습니다 — 옮길 파일이 없습니다.")
        return 0

    files = sorted(ROI_DIR.glob("*.json"))
    if not files:
        print(f"[backfill] {ROI_DIR} 에 .json 파일이 없습니다.")
        return 0

    db = get_session()
    moved, skipped_has_db, skipped_no_cam, skipped_empty = 0, 0, 0, 0
    try:
        for path in files:
            camera_id = path.stem
            cam = C.get(db, camera_id)
            if cam is None:
                print(f"  [건너뜀] {camera_id}: DB에 등록된 카메라가 없습니다.")
                skipped_no_cam += 1
                continue

            existing = cam.roi_row(Domain.FLOOD.value)
            if existing is not None and any((existing.shapes or {}).values()):
                print(f"  [건너뜀] {camera_id}: DB에 이미 flood ROI가 있습니다 "
                     "(웹에서 저장한 값을 보호 — 덮어쓰지 않습니다).")
                skipped_has_db += 1
                continue

            cfg = load_roi_config(path)
            if not (cfg.has_road or cfg.has_low_point or cfg.has_lane_line):
                print(f"  [건너뜀] {camera_id}: 파일에 실제 도형이 없습니다.")
                skipped_empty += 1
                continue

            payload = {
                "frame_width": cfg.frame_width or 0,
                "frame_height": cfg.frame_height or 0,
                "shapes": {
                    "road_roi": cfg.road_roi or [],
                    "low_point_roi": cfg.low_point_roi or [],
                    "lane_threshold_line": cfg.lane_threshold_line or [],
                },
            }
            if args.dry_run:
                print(f"  [반영 예정] {camera_id}: {path.name} -> camera_rois "
                     f"(frame {payload['frame_width']}x{payload['frame_height']}, "
                     f"road_roi {len(cfg.road_roi or [])}조각)")
                moved += 1
                continue

            _, errs = C.save_roi(db, camera_id, Domain.FLOOD.value, payload)
            if errs:
                print(f"  [실패] {camera_id}: {errs}")
                continue
            db.commit()
            print(f"  [반영됨] {camera_id}: {path.name} -> camera_rois")
            moved += 1
    finally:
        db.close()

    print(f"\n[backfill] 완료 — 반영 {moved}건 · 이미 DB에 있어 건너뜀 "
         f"{skipped_has_db}건 · 등록 안 된 카메라 {skipped_no_cam}건 · "
         f"빈 파일 {skipped_empty}건"
         + (" (--dry-run 이라 실제로 저장하지 않았습니다)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
