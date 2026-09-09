"""SVRDD(45° 부감 거리뷰)로 노면 손상 모델을 학습한다.

무엇을 확인하려는 학습인가
    RDD2022 로 두 번(15·50 에폭) 학습했지만 부산 CCTV·근접 영상 모두에서
    **탐지 0건**이었다. 두 번의 실패로 「에폭 부족」 가설은 기각됐고 원인은
    **구도(도메인 갭)** 로 좁혀졌다.

    SVRDD 는 pitch 45° 부감이 섞여 있어, 고가에 달린 관제 CCTV가 내려다보는
    각도에 지금까지 확보한 어떤 데이터보다 가깝다. 그러므로 이 학습의 목적은
    「성능이 얼마나 나오는가」가 아니라 **「각도를 맞추면 신호가 잡히기는
    하는가」** 를 가르는 것이다.

⚠️ 이 환경은 GPU 가 없다 (torch 2.13.0+**cpu**, 16코어)
    8,000장 × 1024px 전량을 CPU 로 수십 에폭 돌리는 것은 며칠짜리 작업이다.
    그래서 기본값을 **빠른 판별(quick)** 로 두었다 — 표본을 줄이고 해상도를
    낮춰 몇 시간 안에 「신호가 잡히는가」만 본다.

    여기서도 0건이면 더 오래 학습해도 달라지지 않을 가능성이 높다(v1→v2 에서
    이미 겪었다). 그때는 데이터가 아니라 **촬영 수단**을 바꿔야 한다는 뜻이다.

Usage:
    python scripts/train_svrdd.py                     # 빠른 판별 (기본)
    python scripts/train_svrdd.py --full              # 전량·고해상도 (GPU 권장)
    python scripts/train_svrdd.py --epochs 30 --imgsz 640
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import resolve_dataset
from tot_dashboard.common.video_io import patch_cv2_imread_for_unicode_paths

# ★ 2026-08-25 — 2026-08-14에 데이터가 D:\dev-PoC_DATA로 옮겨진 뒤로 이
#   경로가 실제로는 빈 폴더였다(학습 화면 신설 중 발견). 프로젝트→보관소
#   순으로 자동 탐색한다.
_yolo_dir = resolve_dataset("road_svrdd_yolo")
DATA_YAML = (_yolo_dir / "data.yaml") if _yolo_dir else (
    PROJECT_ROOT / "data" / "datasets" / "svrdd" / "yolo" / "data.yaml")
RUNS = PROJECT_ROOT / "data" / "datasets" / "svrdd" / "runs"
# 사전학습 가중치에서 출발한다. 맨바닥에서 시작하면 CPU 로는 수렴하지 않는다.
BASE_WEIGHTS = PROJECT_ROOT / "models" / "yolo11s.pt"


def _last_epoch(results_csv: Path) -> int:
    """results.csv 에 기록된 마지막 에폭. 읽지 못하면 0."""
    try:
        lines = [l for l in results_csv.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        return int(float(lines[-1].split(",")[0]))
    except Exception:  # noqa: BLE001
        return 0


def main() -> None:
    # 보관소 경로(D:\dev-PoC_DATA\01_학습데이터_노면\...)의 한글 폴더명을
    # ultralytics 내부 데이터로더가 cv2.imread로 읽다 조용히 실패하는 문제를
    # 막는다(2026-08-25, 침수 학습에서 실제로 겪음).
    patch_cv2_imread_for_unicode_paths()
    ap = argparse.ArgumentParser(description="SVRDD 노면 손상 학습")
    ap.add_argument("--full", action="store_true",
                    help="전량·고해상도로 학습(GPU 권장, CPU 에서는 며칠)")
    ap.add_argument("--epochs", type=int, default=None)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--fraction", type=float, default=None,
                    help="학습셋 중 사용할 비율(0~1). 빠른 판별용")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--name", default=None)
    ap.add_argument("--resume", action="store_true",
                    help="중단된 run 을 이어서 학습한다(weights/last.pt 필요)")
    args = ap.parse_args()

    # ── 이어서 학습 ────────────────────────────────────────────────────────
    # CPU 학습은 에폭당 한 시간이 넘어, 중간에 멈출 일이 실제로 생긴다.
    # 처음부터 다시 돌리면 이미 태운 시간이 통째로 버려진다.
    if args.resume:
        name = args.name or "svrdd_quick"
        last = RUNS / name / "weights" / "last.pt"
        if not last.is_file():
            print(f"[오류] 이어서 학습할 체크포인트가 없습니다: {last}")
            return
        done = _last_epoch(RUNS / name / "results.csv")
        print(f"[svrdd] 이어서 학습 — {name} (완료 {done}에폭까지) · {args.device}")
        print(f"[svrdd] 체크포인트: {last}")
        print("[svrdd] ⚠ 에폭 수·해상도 등은 처음 학습 때의 설정을 그대로 씁니다"
              " (args.yaml).")

        from ultralytics import YOLO

        t0 = time.time()
        YOLO(str(last)).train(resume=True)
        best = RUNS / name / "weights" / "best.pt"
        print(f"\n[svrdd] 학습 완료 — {(time.time() - t0) / 60:.1f}분")
        print(f"[svrdd] 체크포인트: {best}")
        print("\n다음 단계 — 부산 CCTV 프레임에서 실제로 잡히는지 확인:")
        print(f"  python scripts/eval_svrdd_on_cctv.py --model {best}")
        return

    if not DATA_YAML.is_file():
        print(f"[오류] 데이터셋이 없습니다: {DATA_YAML}")
        print("      먼저 실행: python scripts/prepare_svrdd_yolo.py")
        return
    if not BASE_WEIGHTS.is_file():
        print(f"[오류] 사전학습 가중치가 없습니다: {BASE_WEIGHTS}")
        return

    if args.full:
        epochs = args.epochs or 60
        imgsz = args.imgsz or 1024
        fraction = args.fraction or 1.0
        name = args.name or "svrdd_full"
    else:
        # 빠른 판별 — 「신호가 잡히는가」만 본다. 성능 수치를 얻으려는 것이 아니다.
        epochs = args.epochs or 12
        imgsz = args.imgsz or 640
        fraction = args.fraction if args.fraction is not None else 0.25
        name = args.name or "svrdd_quick"

    print(f"[svrdd] {'전량' if args.full else '빠른 판별'} 학습 — "
          f"epochs {epochs} · imgsz {imgsz} · fraction {fraction} · {args.device}")
    if args.device == "cpu":
        print("[svrdd] ⚠ CPU 학습입니다. 진행 상황은 아래 run 폴더에 계속 쌓입니다.")

    from ultralytics import YOLO

    t0 = time.time()
    model = YOLO(str(BASE_WEIGHTS))
    model.train(
        data=str(DATA_YAML),
        epochs=epochs,
        imgsz=imgsz,
        batch=args.batch,
        fraction=fraction,
        device=args.device,
        workers=4,
        project=str(RUNS),
        name=name,
        exist_ok=True,
        patience=0,          # 조기 종료를 끄고 지정한 에폭을 다 돈다
        verbose=True,
    )
    best = RUNS / name / "weights" / "best.pt"
    print(f"\n[svrdd] 학습 완료 — {(time.time() - t0) / 60:.1f}분")
    print(f"[svrdd] 체크포인트: {best}")
    print("\n다음 단계 — 부산 CCTV 프레임에서 실제로 잡히는지 확인:")
    print(f"  python scripts/eval_svrdd_on_cctv.py --model {best}")


if __name__ == "__main__":
    main()
