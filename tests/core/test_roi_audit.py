"""ROI 타당성 진단 (core/roi_audit.py) — 2026-08-23 신설.

## 왜 이 시험이 있나

2026-08-22부터 **저장된 ROI 가 실제 판정에 쓰인다.** 그래서 잘못 그린 ROI 는
곧바로 오판이 된다 — 정체 감시 구역을 보도 위에 그리면 「상시 원활」,
도로 ROI 를 하늘에 그리면 「침수 없음」으로 보인다. 저장 검증은 「꼭짓점
3개 이상」만 보므로 그 위에서 한 번 더 훑는 진단이 필요했다.

지켜야 할 것.

* **정상 ROI 를 잘못 잡지 않는다** — 오탐이 잦으면 아무도 안 본다.
  실제로 첫 구현이 정상 ROI 2건을 「꼬였다」로 잡았고(닫힘용 중복점 때문),
  그 회귀를 여기서 막는다
* 진짜 망가진 도형(면적 0·꼬임·화면 밖)은 **놓치지 않는다**
* 「비었다」와 「잘못 그렸다」를 구분한다
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import roi_audit as RA


# --- 기하 도우미 --------------------------------------------------------------

def test_정상_사각형은_꼬이지_않았다고_본다():
    assert RA.is_self_intersecting([[0, 0], [100, 0], [100, 100], [0, 100]]) is False


def test_나비넥타이는_꼬였다고_잡는다():
    assert RA.is_self_intersecting([[0, 0], [100, 100], [100, 0], [0, 100]]) is True


@pytest.mark.parametrize("poly", [
    # ★ 실제 DB 에 저장돼 있던 값(2026-08-23). 첫 점과 끝 점이 1~3px 차이인
    #   「닫힘용 중복점」 때문에 첫 구현이 오탐했다.
    [[1083, 709], [446, 357], [291, 208], [166, 241], [428, 689], [1083, 708]],
    [[57, 694], [527, 292], [706, 289], [495, 713], [54, 693]],
])
def test_닫힘용_중복점이_있어도_오탐하지_않는다(poly):
    """★ 회귀 방지 — 오탐이 잦으면 진단을 아무도 안 본다."""
    assert RA.is_self_intersecting(poly) is False
    assert RA.polygon_area(poly) > 1000


def test_한_줄로_늘어선_점은_면적이_0():
    assert RA.polygon_area([[0, 0], [50, 50], [100, 100]]) == 0.0


def test_면적은_그리는_방향과_무관하다():
    cw = [[0, 0], [100, 0], [100, 100], [0, 100]]
    ccw = list(reversed(cw))
    assert RA.polygon_area(cw) == RA.polygon_area(ccw) == 10000.0


# --- 진단 --------------------------------------------------------------------

def _roi(shapes, w=1000, h=1000):
    return {"frame_width": w, "frame_height": h, "shapes": shapes}


def _square(x, y, size):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def _codes(audit):
    return {f.code for f in audit.findings}


def test_필수_구역이_비면_오류():
    a = RA.audit_shapes("CAM", "flood", _roi({}))
    assert "MISSING" in _codes(a)
    assert not a.ok


def test_정상_ROI는_아무_지적도_없다():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(100, 100, 500)]}))
    assert a.findings == []
    assert a.ok


def test_찌그러진_다각형은_오류():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [[[0, 0], [50, 50], [100, 100]]]}))
    assert "DEGENERATE" in _codes(a)


def test_꼬인_다각형은_오류():
    bowtie = [[100, 100], [400, 400], [400, 100], [100, 400]]
    a = RA.audit_shapes("CAM", "flood", _roi({"road_roi": [bowtie]}))
    assert "SELF_INTERSECTING" in _codes(a)


def test_화면_밖_다각형은_오류():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(2000, 2000, 100)]}))
    assert "OUT_OF_FRAME" in _codes(a)


def test_너무_작은_구역은_확인_요청():
    """손이 미끄러졌을 수 있다 — 다만 정당할 수도 있어 WARN 이다."""
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(10, 10, 50)]}))  # 0.25%
    assert "TINY" in _codes(a)
    assert all(f.severity != RA.ERROR for f in a.findings)


def test_사실상_화면_전체면_참고만():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(0, 0, 1000)]}))
    assert "HUGE" in _codes(a)
    assert a.ok, "화면 전체는 거를 의도가 없을 뿐 틀린 것은 아니다"


def test_선분은_점_2개여야_한다():
    a = RA.audit_shapes("CAM", "flood", _roi({
        "road_roi": [_square(100, 100, 500)],
        "lane_threshold_line": [[0, 0], [10, 10], [20, 20]]}))
    assert "BAD_LINE" in _codes(a)


def test_저장_해상도를_모르면_확인_요청():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(100, 100, 500)]}, w=0, h=0))
    assert "NO_FRAME_SIZE" in _codes(a)


def test_해상도가_크게_다르면_확인_요청():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(100, 100, 500)]}),
                        current_wh=(320, 240))
    assert "FRAME_MISMATCH" in _codes(a)


def test_해상도가_비슷하면_지적하지_않는다():
    a = RA.audit_shapes("CAM", "flood",
                        _roi({"road_roi": [_square(100, 100, 500)]}),
                        current_wh=(1000, 1000))
    assert "FRAME_MISMATCH" not in _codes(a)


def test_필수가_아닌_도형이_비어도_지적하지_않는다():
    """노면 analysis_roi 는 required=False — 미설정 카메라가 많다."""
    a = RA.audit_shapes("CAM", "road", _roi({}))
    assert a.findings == []


# --- 교통량 검사 --------------------------------------------------------------

def test_차량이_한_대도_안_지나가면_확인_요청():
    roi = [_square(0, 0, 100)]
    # 전부 ROI 밖(하단 중심이 x=500 부근)
    boxes = [(480, 400, 520, 450), (490, 300, 530, 360)]
    f = RA.audit_traffic_overlap(roi, boxes)
    assert f is not None and f.code == "NO_TRAFFIC"
    # ⚠ 한산한 시간대면 정상 ROI 도 0대다 — 그래서 ERROR 가 아니다.
    assert f.severity == RA.WARN


def test_차량이_지나가면_참고로만_남긴다():
    roi = [_square(0, 0, 1000)]
    boxes = [(480, 400, 520, 450)]
    f = RA.audit_traffic_overlap(roi, boxes)
    assert f is not None and f.code == "TRAFFIC_OK"
    assert f.severity == RA.INFO


def test_표본이_없으면_아무_말도_안_한다():
    """차량을 한 대도 못 봤으면 ROI 에 대해 할 말이 없다."""
    assert RA.audit_traffic_overlap([_square(0, 0, 100)], []) is None
    assert RA.audit_traffic_overlap([], [(1, 2, 3, 4)]) is None
