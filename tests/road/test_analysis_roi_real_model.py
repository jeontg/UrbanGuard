"""노면 ROI 필터를 **진짜 모델·진짜 탐지**로 검증한다 (2026-08-23).

## 왜 이 시험이 있나

``test_analysis_roi_filter.py`` 는 탐지기를 몽키패치해 **필터 로직만** 본다.
그것만으로는 "실제 모델이 낸 좌표계와 ROI 좌표계가 정말 맞물리는가"를
증명하지 못한다 — 박스 형식·좌표 기준이 어긋나면 단위시험은 통과하면서
운영에서는 엉뚱한 것을 거른다.

전수점검 당시에는 "노면 모델이 부산 CCTV 실측 탐지 0건이라 검증 불가"로
남겼는데, **그것은 과도하게 비관적인 판단이었다.** 탐지가 0건인 것은
**부산 CCTV 도메인**에서일 뿐, 모델이 학습한 도메인(RDD2022)에서는 정상적으로
탐지한다(실측: val 30장 중 5장에서 6건). **ROI 필터는 영상 도메인과 무관**하므로,
학습 도메인 이미지로 end-to-end 검증이 가능하다.

⚠️ 이 시험이 증명하는 것과 아닌 것
    증명한다 — 실제 모델이 낸 박스 좌표가 ROI 폴리곤과 같은 좌표계에서
    올바르게 걸러진다는 것.
    증명하지 않는다 — 부산 CCTV에서 노면 손상을 잘 찾는다는 것(그것은
    3-6 항목, 모델 교체 과제다).
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("ultralytics")

from tot_dashboard.road.live_analyzer import RoadDefectAnalyzer  # noqa: E402

# 학습 도메인 검증셋 — 보관소에 있다(D:\dev-PoC_DATA, 저장소에는 넣지 않는다).
_VAL_DIRS = [
    Path(r"D:\dev-PoC_DATA\01_학습데이터_노면\rdd2022_czech\yolo\val\images"),
]
_MODEL = Path(r"D:\dev-PoC\UrbanGuard\data\datasets\rdd2022_czech"
              r"\runs\road_defect_proto_v2\weights\best.pt")
_MAX_SCAN = 40   # 탐지가 나오는 이미지를 찾을 때까지 훑을 최대 장수


def _detecting_frame():
    """모델이 실제로 무언가 탐지하는 프레임 하나와 그 탐지 결과를 찾는다."""
    import cv2

    if not _MODEL.is_file():
        pytest.skip(f"노면 모델이 없습니다: {_MODEL}")
    val = next((d for d in _VAL_DIRS if d.is_dir()), None)
    if val is None:
        pytest.skip("학습 도메인 검증셋(보관소)이 이 장비에 없습니다")

    analyzer = RoadDefectAnalyzer()
    # 실제 운영과 같은 경로로 모델을 물린다.
    if not analyzer.set_model(str(_MODEL)):
        pytest.skip("모델 적재에 실패했습니다")

    for p in sorted(glob.glob(str(val / "*.jpg")))[:_MAX_SCAN]:
        frame = cv2.imread(p)
        if frame is None:
            continue
        found = analyzer._analyze_frames([(0, frame)], conf=0.25, roi=None)
        if found:
            return analyzer, frame, found
    pytest.skip(f"검증셋 앞 {_MAX_SCAN}장에서 탐지가 하나도 없습니다")


def _roi_covering(defects, frame, *, include: bool):
    """첫 탐지의 중심을 **포함하는/제외하는** 사각형 ROI 를 만든다."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = defects[0]["box"]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    if include:
        # 그 중심을 넉넉히 감싸는 사각형
        pad = 30
        box = [[max(0, int(cx - pad)), max(0, int(cy - pad))],
               [min(w - 1, int(cx + pad)), max(0, int(cy - pad))],
               [min(w - 1, int(cx + pad)), min(h - 1, int(cy + pad))],
               [max(0, int(cx - pad)), min(h - 1, int(cy + pad))]]
    else:
        # 그 중심에서 확실히 떨어진 반대편 구석
        far_x = 0 if cx > w / 2 else w - 1
        far_y = 0 if cy > h / 2 else h - 1
        sx, sy = (0, 0) if far_x == 0 else (w - 21, 0)
        if far_y != 0:
            sy = h - 21
        box = [[sx, sy], [sx + 20, sy], [sx + 20, sy + 20], [sx, sy + 20]]
    return {"analysis_roi": [box],
            "roi_frame_width": w, "roi_frame_height": h}


def test_실제_모델_탐지가_ROI_안이면_남는다():
    analyzer, frame, baseline = _detecting_frame()
    roi = _roi_covering(baseline, frame, include=True)
    out = analyzer._analyze_frames([(0, frame)], conf=0.25, roi=roi)
    assert out, "ROI 가 탐지를 감싸는데도 전부 걸러졌다 — 좌표계가 어긋났을 수 있다"
    assert out[0]["box"] == baseline[0]["box"]


def test_실제_모델_탐지가_ROI_밖이면_빠진다():
    analyzer, frame, baseline = _detecting_frame()
    roi = _roi_covering(baseline, frame, include=False)
    out = analyzer._analyze_frames([(0, frame)], conf=0.25, roi=roi)
    kept = {tuple(d["box"]) for d in out}
    assert tuple(baseline[0]["box"]) not in kept, (
        "ROI 밖 탐지가 그대로 남았다 — 필터가 실제로는 안 걸리고 있다")


def test_ROI_없으면_실제_모델_결과가_그대로_나온다():
    """★ 하위호환 — ROI 미설정 카메라가 갑자기 0건이 되면 안 된다."""
    analyzer, frame, baseline = _detecting_frame()
    out = analyzer._analyze_frames([(0, frame)], conf=0.25, roi=None)
    assert len(out) == len(baseline)
