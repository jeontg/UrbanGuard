"""Road-surface defect (pothole/crack) detection — Phase 2 prototype.

Wraps a YOLO **object-detection** model the same way ``flood.water_segmentation``
wraps the water-seg model. Bounding boxes (not instance masks) are used because
the public pothole/crack datasets surveyed for this project are box-annotated,
unlike flood's polygon water masks — see
docs/road_surface_management_plan.md section 2.

No trained weights ship with this module. Point ``model_path`` at a checkpoint
trained on a public dataset, or a fine-tuned one once available (Phase 3).

⚠ LICENSE NOTE (docs/road_surface_management_plan.md 2-1절 참고): 후보로 조사한
공개 포트홀/균열 데이터셋들은 데이터셋마다 라이선스가 다르고, 일부(RDD2022 등)는
정확한 재배포/상업이용 조건이 이번 조사에서 명확히 확인되지 않았다 — 실제 학습에
쓰기 전 데이터셋 라이선스를 반드시 개별 재확인해야 한다. 또한 이 프로젝트가
이미 사용 중인 Ultralytics YOLOv8/YOLO11 자체가 AGPL-3.0으로 배포되어, 네트워크
서비스(SaaS)로 제공 시 소스 공개 의무가 발생할 수 있다 — 상업적 배포 전
Ultralytics Enterprise License 필요 여부를 법무팀이 확인해야 한다(이는
flood/object_detection 등 기존 모듈에도 이미 해당하는 프로젝트 전체의 이슈이며,
이번 도로 도메인에서 새로 생긴 문제는 아니다).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 실제 학습 모델의 클래스 순서에 맞춰 조정 필요 — 사전학습 모델을 바꾸면 값이 달라진다.
DEFAULT_CLASS_NAMES = {0: "pothole", 1: "crack"}


@dataclass
class Defect:
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 (원본 프레임 픽셀 좌표)
    cls_name: str
    confidence: float


@dataclass
class DefectResult:
    defects: list[Defect] = field(default_factory=list)
    raw: Any = None  # 오버레이용 ultralytics Results 원본

    @property
    def count(self) -> int:
        return len(self.defects)


def detect_defects(
    model,
    frame: np.ndarray,
    conf: float = 0.25,
    iou: float = 0.50,
    imgsz: int = 640,
    device: Any = "cpu",
    class_names: dict[int, str] | None = None,
) -> DefectResult:
    """단일 BGR 프레임에서 포트홀/균열을 탐지한다.

    ``model``은 ``common.models_loader``로 이미 로드된 ultralytics YOLO
    detection 모델(``flood.water_segmentation.segment_water``와 동일한
    호출 패턴).
    """
    names = class_names or DEFAULT_CLASS_NAMES
    results = model.predict(
        frame, conf=conf, iou=iou, imgsz=imgsz, device=device, verbose=False
    )
    result = results[0]
    defects: list[Defect] = []
    boxes = getattr(result, "boxes", None)
    if boxes is not None:
        for b in boxes:
            xyxy = b.xyxy[0].tolist()
            cls_id = int(b.cls[0])
            defects.append(Defect(
                box=(int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])),
                cls_name=names.get(cls_id, str(cls_id)),
                confidence=float(b.conf[0]),
            ))
    return DefectResult(defects=defects, raw=result)
