"""교통위험 — 낙하물·화재연기(YOLO) 자체 학습. Phase 7 진입점.

## 현재 상태 — 학습 데이터가 없다 (2026-08-26 확인)

낙하물·화재연기는 **COCO에 없는 클래스**라 사전학습 모델로 대체할 수 없다
(``core/vocabulary.py``의 ``traffic_debris``·``traffic_fire_smoke``가
``detectable=False``인 근거). ``D:\\dev-PoC_DATA\\07_학습데이터_교통\\
traffic_incident_own\\`` 폴더 자체가 아직 비어 있다.

이 스크립트는 ``scripts/train_traffic_vehicle_yolo.py``와 **같은 존재확인
+ 거부 패턴**을 그대로 따른다 — 라벨링된 데이터가 없으면 실행 자체가
거부된다. 데이터 없이 「학습했다」는 결과를 내면 안 되기 때문이다.

## 데이터가 준비되면

``data/datasets/traffic_incident_own/yolo/`` (또는 학습 데이터 보관소
``D:\\dev-PoC_DATA\\07_학습데이터_교통\\traffic_incident_own\\yolo\\``)에
Ultralytics YOLO 형식(``images/`` · ``labels/`` · ``data.yaml``, 클래스
``debris``·``fire_smoke`` — ``scripts/label_traffic_incident.py``가 내는
포맷 그대로)으로 채워 넣으면 이 스크립트가 그대로 읽는다. 코드 수정이
필요 없다.

⚠️ **데이터 출처는 이 스크립트가 단정하지 않는다** — 어떤 공개 데이터셋을
쓸지는 라이선스 검토 문서(``docs/<생성일시>/
traffic_incident_dataset_license_investigation.md``)를 먼저 확인하고,
법무 검토 후에 결정해야 한다.

⚠️ **학습이 끝나도 곧바로 「탐지 가능」이 되지 않는다** — 모델 파일이
생기면 ``core/model_registry.py``가 자동으로 찾아 교통 도메인에 등록하지만
(``_DOMAIN_HINTS``에 "debris"·"fire"·"smoke" 키워드 등록됨),
``core/vocabulary.py``의 ``detectable`` 플래그를 True로 바꾸고 실제
판정 파이프라인에 배선하는 것은 **이번 회차 범위 밖의 별도 작업**이다
(성능 검증 선행 필요 — ``docs/road_surface_management_plan.md``에서
검증 안 된 모델을 배선했다가 겪은 오탐 사례 참고).

Usage:
    python scripts/train_traffic_incident_yolo.py --epochs 30
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.data_archive import dataset_hint, resolve_dataset
from tot_dashboard.common.video_io import patch_cv2_imread_for_unicode_paths

RUNS = PROJECT_ROOT / "data" / "training_runs" / "traffic_incident"
BASE_WEIGHTS_DEFAULT = PROJECT_ROOT / "models" / "yolo11s.pt"


def main() -> None:
    # 데이터가 준비되면 보관소(D:\dev-PoC_DATA\07_학습데이터_교통\...)의
    # 한글 폴더명 아래 놓일 것이다. ultralytics YOLO의 내부 데이터로더도
    # cv2.imread를 쓰므로 미리 전역 패치해 둔다(train_traffic_vehicle_yolo.py
    # 와 동일 — 2026-08-25 침수 학습에서 실제로 겪은 문제).
    patch_cv2_imread_for_unicode_paths()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-weights", default=None,
                    help="시작 가중치(생략 시 yolo11s.pt 사전학습)")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--imgsz", type=int, default=416)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    data_dir = resolve_dataset("traffic_incident_yolo")
    if data_dir is None:
        raise SystemExit(
            "[오류] 낙하물·화재연기 학습 데이터가 없습니다.\n"
            f"       다음 위치에 YOLO 형식으로 채워 넣으십시오: "
            f"{dataset_hint('traffic_incident_yolo')}\n"
            "       (scripts/collect_traffic_incident_frames.py 로 배경 프레임을, "
            "scripts/extract_traffic_incident_frames.py 로 사고·화재 영상을 "
            "모은 뒤 scripts/label_traffic_incident.py 로 라벨링하세요.)")
    data_yaml = data_dir / "data.yaml"
    if not data_yaml.is_file():
        raise SystemExit(f"[오류] data.yaml이 없습니다: {data_yaml}")

    base = Path(args.base_weights) if args.base_weights else BASE_WEIGHTS_DEFAULT
    if not base.is_file():
        raise SystemExit(f"[오류] 시작 가중치가 없습니다: {base}")

    name = args.name or f"traffic_incident_{int(time.time())}"
    print(f"[traffic-incident-train] data={data_yaml} base={base.name} "
          f"epochs={args.epochs} imgsz={args.imgsz} device={args.device}")

    from ultralytics import YOLO

    t0 = time.time()
    model = YOLO(str(base))
    model.train(
        data=str(data_yaml),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=4,
        project=str(RUNS),
        name=name,
        exist_ok=True,
        patience=0,
        verbose=True,
    )
    best = RUNS / name / "weights" / "best.pt"
    print(f"\n[traffic-incident-train] 완료 ({(time.time() - t0) / 60:.1f}분) -> {best}")


if __name__ == "__main__":
    main()
