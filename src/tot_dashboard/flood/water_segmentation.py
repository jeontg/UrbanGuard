"""Water-surface segmentation using the custom YOLO11-seg model (``models/best.pt``).

Canonical version adopted from flood3's ``flood/water_segmentation.py``, which
is underpath_flood_dashboard's original module plus a corrupted-frame
detector added for live HLS streams. Confirmed identical core logic between
the two originals during the integration review; the only behavioral
difference was the default confidence (0.25 in underpath_flood_dashboard's
function signature vs. 0.10 in flood3's — both projects actually run at 0.10
via their config files, per underpath_flood_dashboard's README calibration
notes, so 0.10 is adopted as the function default here too).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


def is_likely_corrupted_frame(frame: np.ndarray, tile: int = 16,
                              flat_std_thresh: float = 4.0,
                              flat_area_frac_thresh: float = 0.20) -> bool:
    """Heuristic for H.264 decode corruption on live HLS streams.

    Splits the frame into ``tile``x``tile`` blocks, computes each block's
    brightness std-dev, and flags the frame as corrupted if too large a
    fraction of blocks are near-flat (error-concealment artifact). Segmenting
    a corrupted frame as-is can misclassify decode-corruption noise as water.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    h2, w2 = (h // tile) * tile, (w // tile) * tile
    if h2 < tile or w2 < tile:
        return False
    gray = gray[:h2, :w2]
    tiles = gray.reshape(h2 // tile, tile, w2 // tile, tile).swapaxes(1, 2)
    stds = tiles.std(axis=(2, 3))
    flat_fraction = float((stds < flat_std_thresh).mean())
    return flat_fraction > flat_area_frac_thresh


@dataclass
class WaterResult:
    mask: np.ndarray            # uint8 (H, W), 0 or 255 = flood_water
    num_instances: int          # number of water polygons detected
    water_pixels: int           # total water pixels in the full frame
    max_confidence: float       # highest instance confidence (0 if none)
    raw: Any = None             # underlying ultralytics Results (for overlays)


def segment_without_letterbox(segment_fn, frame: np.ndarray) -> WaterResult:
    """레터박스(위아래·좌우 검은 여백)를 뺀 영역만 세그멘테이션한다.

    ★ 왜 필요한가 — 학습 데이터에 레터박스 영상이 없어 **모델이 검은 여백을
    물로 오인한다**(2026-08-25 실측: 레터박스 오탐 17.9%). 여백을 잘라내고
    추론하면 이 오탐이 사라진다.

    ⚠️ **좌표계를 원본으로 되돌리는 것이 이 함수의 핵심이다.** 잘라낸 채로
    마스크를 돌려주면 ROI 폴리곤·`mask_bbox()`(증거 상자)가 전부 어긋난다 —
    이 저장소가 반복해서 겪은 「해상도·좌표 불일치」 함정과 같은 부류다
    (`flow_propose`의 `scale_polygons` 주석 참고). 그래서 잘라낸 마스크를
    원본 크기의 0 배열 제자리에 다시 붙여서 돌려준다.

    ``water_pixels``·``num_instances``·``max_confidence``는 잘라낸 결과의
    값을 그대로 쓴다 — 여백은 애초에 물일 수 없으므로 그 편이 정확하다.

    레터박스가 없으면(라이브 CCTV가 대부분 그렇다) 잘라내지 않고 그대로
    추론한다 — 즉 이 함수는 그 경우 아무 영향이 없다.
    """
    from ..common.video_io import detect_letterbox

    h, w = frame.shape[:2]
    top, bottom, left, right = detect_letterbox(frame)
    if (top, bottom, left, right) == (0, h, 0, w):
        return segment_fn(frame)          # 여백 없음 — 기존 동작 그대로

    res = segment_fn(frame[top:bottom, left:right])
    full = np.zeros((h, w), dtype=np.uint8)
    full[top:bottom, left:right] = res.mask
    return WaterResult(mask=full, num_instances=res.num_instances,
                       water_pixels=res.water_pixels,
                       max_confidence=res.max_confidence, raw=res.raw)


def _masks_to_binary(result, height: int, width: int) -> tuple[np.ndarray, int]:
    """Build a full-res 0/255 mask from a YOLO-seg Results object.

    Uses ``masks.xy`` (polygons already in original-image coordinates) which
    avoids letterbox/resize ambiguity.
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    masks = getattr(result, "masks", None)
    if masks is None or masks.xy is None or len(masks.xy) == 0:
        return mask, 0
    count = 0
    for poly in masks.xy:
        if poly is None or len(poly) < 3:
            continue
        cv2.fillPoly(mask, [np.asarray(poly, dtype=np.int32)], 255)
        count += 1
    return mask, count


def segment_water(
    model,
    frame: np.ndarray,
    conf: float = 0.10,
    iou: float = 0.50,
    imgsz: int = 640,
    device: Any = "cpu",
) -> WaterResult:
    """Run water segmentation on a single BGR frame."""
    h, w = frame.shape[:2]
    results = model.predict(
        frame, conf=conf, iou=iou, imgsz=imgsz, device=device, verbose=False
    )
    result = results[0]
    mask, n = _masks_to_binary(result, h, w)

    max_conf = 0.0
    if getattr(result, "masks", None) is not None and result.boxes is not None:
        try:
            confs = result.boxes.conf
            if confs is not None and len(confs) > 0:
                max_conf = float(confs.max())
        except Exception:
            max_conf = 0.0

    return WaterResult(
        mask=mask,
        num_instances=n,
        water_pixels=int(np.count_nonzero(mask)),
        max_confidence=max_conf,
        raw=result,
    )


def mask_bbox(mask: "np.ndarray") -> tuple[int, int, int, int] | None:
    """물로 판정된 픽셀들의 경계 상자. 물이 없으면 ``None``.

    ★ **왜 이 값인가 (S-88 증거 팝업, 2026-08-20)**

    「이벤트가 발생한 부분」을 표시해 달라는 요구를 받았다. 침수는 개별
    차량·사람을 탐지하는 것이 아니라 **영역을 세그멘테이션**하므로, 사람이
    쓰는 뜻의 「탐지 상자」가 원래 없다.

    ⚠️ **지어내지 않는다.** 그래서 실제 판정 결과인 **마스크 자체의
    경계**를 상자로 쓴다 — 이것은 세그멘테이션 모델이 「물이라고 실제로
    표시한 픽셀」의 범위이지, 임의로 그린 값이 아니다.
    """
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
