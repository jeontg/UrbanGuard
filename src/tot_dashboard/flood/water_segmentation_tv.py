"""물 세그멘테이션 — torchvision(BSD) 시맨틱 세그멘테이션 백엔드.

기존 ``water_segmentation.py``(Ultralytics YOLO11-seg, **AGPL-3.0**)의 대체
구현이다. ``WaterResult``를 그대로 반환하므로 하위 코드(metrics_core/risk_engine/
visualization)는 수정 없이 동작한다.

## 왜 이 백엔드인가

- **라이선스**: torchvision은 BSD. AGPL 의무가 없어 상용 납품에 적합
  (docs/yolo_license_alternatives.md)
- **성능**: 자체 구축 데이터셋(1,570장)으로 학습해 val F1 0.9392 / IoU 0.8854.
  기존 모델이 **전혀 검출하지 못하던** 실제 침수 영상에서 정상 검출
  (docs/flood_water_dataset_workflow.md 3-B절)
- **CPU 실행**: 경량 MobileNetV3 기반이라 GPU 없이 학습·추론 가능

## 인스턴스 개수에 대해

원본 YOLO-seg는 인스턴스 세그멘테이션이라 물 덩어리를 개별 폴리곤으로 냈지만,
파이프라인은 ``num_instances``를 판단에 쓰지 않는다(전량 이진 마스크 기반).
여기서는 표시 목적으로만 연결요소(connected component) 개수를 센다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch

from .water_segmentation import WaterResult

_MIN_BLOB_FRAC = 0.0005  # 전체 화면 대비 이 비율 미만 조각은 노이즈로 무시


def load_tv_water_model(ckpt_path: str | Path, device: Any = "cpu"):
    """``scripts/train_flood_water_cpu.py``가 저장한 체크포인트를 로드.

    반환값은 ``segment_water_tv``의 첫 인자로 그대로 넘긴다.
    """
    from torchvision.models.segmentation import (deeplabv3_mobilenet_v3_large,
                                                 lraspp_mobilenet_v3_large)

    ck = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
    arch = ck.get("arch", "lraspp")
    builder = lraspp_mobilenet_v3_large if arch == "lraspp" else deeplabv3_mobilenet_v3_large
    model = builder(weights=None, num_classes=2)
    model.load_state_dict(ck["model"])
    model.eval()
    dev = torch.device(device if str(device) != "auto" else "cpu")
    model.to(dev)
    # 학습 시 입력 크기를 체크포인트에 함께 저장해두었다 - 추론도 같은 크기로 맞춘다
    model._tv_input_size = int(ck.get("size", 384))  # type: ignore[attr-defined]
    model._tv_device = dev                            # type: ignore[attr-defined]
    return model


@torch.no_grad()
def segment_water_tv(
    model,
    frame: np.ndarray,
    conf: float = 0.5,
    iou: float = 0.50,          # 인터페이스 호환용(시맨틱 세그에는 미사용)
    imgsz: int | None = None,
    device: Any = "cpu",        # 인터페이스 호환용(모델 로드 시 이미 결정됨)
) -> WaterResult:
    """단일 BGR 프레임의 물 영역 세그멘테이션.

    ``conf``는 물(전경) 클래스의 **확률 임계값**(기본 0.5). 값을 낮추면 더
    민감해진다.
    """
    h, w = frame.shape[:2]
    size = imgsz or getattr(model, "_tv_input_size", 384)
    dev = getattr(model, "_tv_device", torch.device("cpu"))

    x = cv2.resize(frame, (size, size))
    x = torch.from_numpy(x.transpose(2, 0, 1).astype(np.float32) / 255.0).unsqueeze(0).to(dev)
    logits = model(x)["out"]
    prob = torch.softmax(logits, dim=1)[0, 1]        # 물 클래스 확률
    prob_np = prob.cpu().numpy()

    small = (prob_np >= float(conf)).astype(np.uint8)
    mask = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

    # 작은 노이즈 조각 제거 + 인스턴스 개수(표시용)
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    min_area = _MIN_BLOB_FRAC * h * w
    keep = np.zeros_like(mask)
    n_inst = 0
    for i in range(1, n_labels):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep[labels == i] = 255
            n_inst += 1

    water_px = int(np.count_nonzero(keep))
    max_conf = float(prob_np.max()) if water_px else 0.0
    return WaterResult(mask=keep, num_instances=n_inst,
                       water_pixels=water_px, max_confidence=max_conf, raw=None)
