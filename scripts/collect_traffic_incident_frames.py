"""Phase 7 - 낙하물·화재연기 데이터 수집: 실시간 CCTV 프레임을 모은다.

낙하물·화재연기는 COCO에 없는 클래스라 자체 학습 없이는 탐지할 수 없다
(``core/vocabulary.py``의 ``traffic_debris``·``traffic_fire_smoke``,
``detectable=False`` 근거). 이 스크립트는 그 학습의 첫 단계 — 라벨링할
원본 프레임을 모으는 것만 한다(라벨링 자체는
``scripts/label_traffic_incident.py``).

``scripts/collect_road_cctv_frames.py``와 **의도적으로 같은 패턴**을 쓴다 —
``configs/blocks.json``에 등록된 블록의 HLS 스트림에서 한 번씩 프레임을 받아
저장한다.

⚠️ **이 스크립트만으로는 낙하물·화재연기 장면을 거의 못 모은다.** 두 사건은
드문 돌발 상황이라, 평시 스트림을 아무리 자주 찍어도 대부분 **배경(정상)
프레임**만 쌓인다. 그런데 그게 오히려 필요하다 — 오탐을 줄이려면 배경 샘플이
반드시 있어야 한다(``docs/flood_water_dataset_workflow.md``에서 배경 샘플
부족이 실제 오탐으로 이어진 사례 참고). **실제 낙하물·화재연기 장면**을
모으려면 이미 확보한 사고·화재 영상을 ``scripts/extract_traffic_incident_
frames.py``로 프레임 추출해야 한다 — 이 스크립트를 여러 시점에 반복 실행해
배경을 계속 쌓아가는 방식으로 함께 쓸 것을 의도했다.

저장 위치: data/datasets/traffic_incident_own/raw/live/<block_id>/<timestamp>.jpg
(gitignore 처리됨 -- data/datasets/ 는 커밋하지 않는다)

Usage:
    python scripts/collect_traffic_incident_frames.py                  # 전체 블록
    python scripts/collect_traffic_incident_frames.py --block BLOCK-OLYMPIC
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2

from tot_dashboard.common.config import PROJECT_ROOT

OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "traffic_incident_own" / "raw" / "live"


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
            print(f"[collect_traffic_incident_frames] 경고: blocks.json에 없는 id 무시됨: {missing}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    saved, failed = [], []
    for b in blocks:
        src = b.get("source") or {}
        if src.get("type") != "hls" or not src.get("url"):
            print(f"[collect_traffic_incident_frames] {b['id']}: HLS 소스 아님, 건너뜀")
            continue
        print(f"[collect_traffic_incident_frames] {b['id']}({b['name']}) 프레임 수집 중...")
        frame = _grab_hls_frame(src["url"])
        if frame is None:
            print(f"[collect_traffic_incident_frames]   실패: {src['url']}")
            failed.append(b["id"])
            continue
        out_dir = OUT_ROOT / b["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{ts}.jpg"
        cv2.imwrite(str(out_path), frame)
        print(f"[collect_traffic_incident_frames]   저장: {out_path} ({frame.shape[1]}x{frame.shape[0]})")
        saved.append(str(out_path))

    print(f"\n[collect_traffic_incident_frames] 완료 - 저장 {len(saved)}건, 실패 {len(failed)}건")
    if failed:
        print(f"[collect_traffic_incident_frames] 실패한 블록: {failed}")
    print("[collect_traffic_incident_frames] 이 스크립트는 배경(정상) 프레임 위주로 모읍니다 — "
         "낙하물·화재연기 실제 장면은 scripts/extract_traffic_incident_frames.py로 "
         "확보한 사고·화재 영상에서 추출하세요.")


if __name__ == "__main__":
    main()
