"""Phase 3 - 자체 CCTV 데이터 수집: 실제 부산 도로 CCTV에서 프레임을 모은다.

Phase 2에서 공개 데이터셋(RDD2022) 기반 모델이 실제 부산 CCTV에서 전혀
탐지하지 못한다는 게 확인됐다(도메인 갭, docs/road_surface_management_plan.md
Phase 2 "핵심 발견"/"추가 검증" 참고). 실제 배포 환경과 촬영 조건이 일치하는
자체 데이터가 없으면 이 문제는 풀리지 않을 것으로 판단되어, 자체 CCTV 데이터
라벨링(Phase 3)을 시작한다. 이 스크립트는 그 첫 단계 - 라벨링할 원본 프레임을
모으는 것만 한다(라벨링 자체는 scripts/label_road_defects.py).

configs/blocks.json에 등록된 모든 블록(또는 --block으로 지정한 일부)의 HLS
스트림에서 한 번씩 프레임을 받아 저장한다. 시간대/날씨/교통 상황에 따라
노면이 다르게 보이므로, **이 스크립트를 여러 시점(맑을 때/비 온 뒤/야간 등)에
반복 실행**해 프레임을 계속 쌓아가는 방식을 의도했다 - 한 번 실행으로 끝나는
게 아니라 주기적으로 재실행해야 한다.

저장 위치: data/datasets/road_cctv_own/raw/<block_id>/<timestamp>.jpg
(gitignore 처리됨 -- data/datasets/ 는 커밋하지 않는다)

Usage:
    python scripts/collect_road_cctv_frames.py                  # 전체 블록
    python scripts/collect_road_cctv_frames.py --block BLOCK-OLYMPIC --block BLOCK-BEXCO2
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2

from tot_dashboard.common.config import PROJECT_ROOT

OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "raw"


def _load_blocks() -> list[dict]:
    path = PROJECT_ROOT / "configs" / "blocks.json"
    return json.loads(path.read_text(encoding="utf-8"))["blocks"]


def _grab_hls_frame(url: str, timeout_tries: int = 30):
    cap = cv2.VideoCapture(url)
    frame = None
    if cap.isOpened():
        for _ in range(timeout_tries):
            ok, f = cap.read()
            if ok:
                frame = f
                break
        cap.release()
    return frame


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--block", action="append", default=None,
                    help="configs/blocks.json 블록 id (여러 번 지정 가능, 생략 시 전체 블록)")
    args = ap.parse_args()

    blocks = _load_blocks()
    if args.block:
        wanted = set(args.block)
        blocks = [b for b in blocks if b["id"] in wanted]
        missing = wanted - {b["id"] for b in blocks}
        if missing:
            print(f"[collect_road_cctv_frames] 경고: blocks.json에 없는 id 무시됨: {missing}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    saved, failed = [], []
    for b in blocks:
        src = b.get("source") or {}
        if src.get("type") != "hls" or not src.get("url"):
            print(f"[collect_road_cctv_frames] {b['id']}: HLS 소스 아님, 건너뜀")
            continue
        print(f"[collect_road_cctv_frames] {b['id']}({b['name']}) 프레임 수집 중...")
        frame = _grab_hls_frame(src["url"])
        if frame is None:
            print(f"[collect_road_cctv_frames]   실패: {src['url']}")
            failed.append(b["id"])
            continue
        out_dir = OUT_ROOT / b["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{ts}.jpg"
        cv2.imwrite(str(out_path), frame)
        print(f"[collect_road_cctv_frames]   저장: {out_path} ({frame.shape[1]}x{frame.shape[0]})")
        saved.append(str(out_path))

    print(f"\n[collect_road_cctv_frames] 완료 - 저장 {len(saved)}건, 실패 {len(failed)}건")
    if failed:
        print(f"[collect_road_cctv_frames] 실패한 블록: {failed}")
    print("[collect_road_cctv_frames] 시간대/날씨를 달리해 이 스크립트를 반복 실행하면 데이터가 쌓입니다.")


if __name__ == "__main__":
    main()
