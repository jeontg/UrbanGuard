# -*- coding: utf-8 -*-
"""기존 `configs/blocks.json` + `configs/roi/*.json` → DB(cameras) 이관.

한 번만 실행하는 스크립트다. 기존 설정을 **읽기만** 하고 지우지 않는다 —
이관이 잘못돼도 원본으로 되돌릴 수 있어야 한다.

실행::

    .\\.venv\\Scripts\\python.exe scripts\\migrate_blocks_to_cameras.py            # 미리보기
    .\\.venv\\Scripts\\python.exe scripts\\migrate_blocks_to_cameras.py --apply    # 실제 반영

도메인 배정 기본값
  - 침수: 기존 blocks.json 항목 전부. **상시 분석 켬**(지금 동작과 동일)
  - 인파: `crowd` 설정이 있던 카메라만 사용 켬. **선택 분석**
  - 노면: 전 카메라 사용 켬. **선택 분석**(기존 도로 화면이 전 지점을 대상으로 함)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tot_dashboard.core import cameras as C  # noqa: E402
from tot_dashboard.core.db import get_session  # noqa: E402
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi  # noqa: E402
from tot_dashboard.core.roles import Domain  # noqa: E402

BLOCKS = ROOT / "configs" / "blocks.json"
ROI_DIR = ROOT / "configs" / "roi"

# 카메라가 아니라 침수 도메인 설정인 항목들
FLOOD_KEYS = ("rainfall", "river", "rain", "road_attrs", "low_point", "risk")


def load_blocks() -> list[dict]:
    with open(BLOCKS, encoding="utf-8") as f:
        return json.load(f).get("blocks", [])


def load_roi(block_id: str) -> dict | None:
    p = ROI_DIR / f"{block_id}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def plan() -> list[dict]:
    out = []
    for b in load_blocks():
        src = b.get("source") or {}
        coord = b.get("coordinates") or {}
        crowd = b.get("crowd") or {}
        roi = load_roi(b.get("id", ""))
        out.append({
            "id": b.get("id"),
            "name": b.get("name") or b.get("id"),
            "dept": b.get("dept") or "",
            "lat": coord.get("lat"), "lng": coord.get("lng"),
            "source_type": src.get("type") or "synthetic",
            "source_url": src.get("url") or "",
            "source_path": src.get("path") or "",
            "cctv_name": src.get("cctv_name") or "",
            "flood_config": {k: b[k] for k in FLOOD_KEYS if k in b},
            "crowd_config": {k: v for k, v in crowd.items()
                             if k != "intrusion_roi"},
            "crowd_enabled": bool(crowd),
            "intrusion_roi": crowd.get("intrusion_roi") or [],
            "roi": roi,
        })
    return out


def show(rows: list[dict]) -> None:
    print(f"\n이관 대상 카메라 {len(rows)}개\n")
    print(f"  {'ID':22s} {'소스':10s} {'침수ROI':8s} {'인파':6s} {'노면':6s}")
    print("  " + "-" * 60)
    for r in rows:
        roi = r["roi"] or {}
        nroad = len(roi.get("road_roi") or [])
        print(f"  {r['id']:22s} {r['source_type']:10s} "
              f"{('도로 '+str(nroad)) if nroad else '없음':8s} "
              f"{'사용' if r['crowd_enabled'] else '-':6s} {'사용':6s}")
    print()
    print("  도메인 기본값:")
    print("    침수 = 사용 + 상시 분석")
    print("    인파 = crowd 설정이 있던 카메라만 사용, 선택 분석")
    print("    노면 = 전 카메라 사용, 선택 분석")


def apply(rows: list[dict]) -> None:
    db = get_session()
    try:
        existing = {c.id for c in C.list_all(db)}
        if existing:
            print(f"  [중단] 이미 카메라 {len(existing)}개가 등록돼 있습니다: "
                  f"{sorted(existing)}")
            print("         중복 이관을 막기 위해 진행하지 않습니다.")
            return

        for r in rows:
            cam = Camera(
                id=r["id"], name=r["name"], dept=r["dept"],
                lat=r["lat"], lng=r["lng"], source_type=r["source_type"],
                source_url=r["source_url"], source_path=r["source_path"],
                cctv_name=r["cctv_name"], is_active=True,
                note="blocks.json 에서 이관")
            db.add(cam)
            db.flush()

            db.add(CameraDomain(camera_id=cam.id, domain=Domain.FLOOD.value,
                                enabled=True, continuous=True,
                                config=r["flood_config"] or None))
            db.add(CameraDomain(camera_id=cam.id, domain=Domain.CROWD.value,
                                enabled=r["crowd_enabled"], continuous=False,
                                config=r["crowd_config"] or None))
            db.add(CameraDomain(camera_id=cam.id, domain=Domain.ROAD.value,
                                enabled=True, continuous=False, config=None))

            roi = r["roi"]
            if roi:
                db.add(CameraRoi(
                    camera_id=cam.id, domain=Domain.FLOOD.value,
                    frame_width=int(roi.get("frame_width") or 0),
                    frame_height=int(roi.get("frame_height") or 0),
                    shapes={
                        "road_roi": roi.get("road_roi") or [],
                        "low_point_roi": roi.get("low_point_roi") or [],
                        "lane_threshold_line": roi.get("lane_threshold_line") or [],
                    }))
            if r["intrusion_roi"]:
                # 인파 침입 ROI 는 프레임 크기 정보가 없어 침수 ROI 값을 빌린다.
                fw = int((roi or {}).get("frame_width") or 0)
                fh = int((roi or {}).get("frame_height") or 0)
                db.add(CameraRoi(
                    camera_id=cam.id, domain=Domain.CROWD.value,
                    frame_width=fw, frame_height=fh,
                    shapes={"intrusion_roi": r["intrusion_roi"]}))
        db.commit()
        # 콘솔이 cp949 라 em-dash 등이 깨진다. ASCII 구두점만 쓴다.
        print(f"\n  이관 완료 : 카메라 {len(rows)}개")
        print("  ⚠ 원본 configs/blocks.json 과 configs/roi/ 는 그대로 두었습니다.")
        print("     동작을 확인한 뒤에 정리하세요.")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"\n  [실패] {e}")
        raise
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="blocks.json → cameras 이관")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 반영")
    args = ap.parse_args()

    if not BLOCKS.exists():
        print(f"[오류] 원본이 없습니다: {BLOCKS}")
        return 1
    rows = plan()
    show(rows)
    if not args.apply:
        print("  미리보기입니다. 반영하려면 --apply 를 붙이세요.\n")
        return 0
    apply(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
