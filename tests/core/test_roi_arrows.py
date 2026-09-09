"""통행 방향 화살표(`flow_arrows`) ROI — 역주행 판정의 기준(Phase 4, 2026-08-26).

## 왜 이 시험이 있나

`flow_arrows`는 기존 폴리곤(``kind="polygon"``)·기준선(``kind="line"``)과
저장 구조는 같은 "점 목록의 목록"이지만, 항목마다 점이 **정확히 2개**
(시작→끝이 곧 방향)여야 한다는 제약이 다르다. 이 제약을 빠뜨리면:

* ``validate_roi``/``save_roi``가 폴리곤 규칙(꼭짓점 3개 이상)을 그대로
  적용해 화살표를 전부 거부하거나, 반대로 아무 검증 없이 저장한다
* ``roi_audit``의 ``len(p) >= 3`` 필터(폴리곤 전용)에 걸려 화살표가
  **진단에서 조용히 사라진다** — Phase 4 계획에서 미리 짚은 함정
"""
from __future__ import annotations

from tot_dashboard.core import cameras as C
from tot_dashboard.core import roi_audit as RA

ARROW_KEY = "flow_arrows"


def _codes(audit):
    return {f.code for f in audit.findings}


# --- validate_roi -------------------------------------------------------------


def test_점_2개짜리_화살표는_통과한다():
    errs = C.validate_roi("traffic", {
        "frame_width": 640, "frame_height": 360,
        "shapes": {"congestion_roi": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                  ARROW_KEY: [[[10, 10], [10, 100]]]}})
    assert errs == []


def test_점이_3개면_거부한다():
    errs = C.validate_roi("traffic", {
        "frame_width": 640, "frame_height": 360,
        "shapes": {"congestion_roi": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                  ARROW_KEY: [[[10, 10], [10, 50], [10, 100]]]}})
    assert any("2개" in e for e in errs)


def test_시작점과_끝점이_같으면_거부한다():
    """길이 0인 화살표는 방향이 없다."""
    errs = C.validate_roi("traffic", {
        "frame_width": 640, "frame_height": 360,
        "shapes": {"congestion_roi": [[[0, 0], [100, 0], [100, 100], [0, 100]]],
                  ARROW_KEY: [[[10, 10], [10, 10]]]}})
    assert any("0" in e or "길이" in e for e in errs)


def test_화살표가_없어도_필수가_아니라_통과한다():
    """flow_arrows 는 required=False — 안 그린 카메라는 역주행 판정을
    그냥 안 하면 된다."""
    errs = C.validate_roi("traffic", {
        "frame_width": 640, "frame_height": 360,
        "shapes": {"congestion_roi": [[[0, 0], [100, 0], [100, 100], [0, 100]]]}})
    assert errs == []


# --- save_roi -------------------------------------------------------------


def test_저장하면_방향_순서가_보존된다():
    """클릭 순서가 곧 방향이다 — 정리 중 뒤바뀌면 반대 방향으로 판정된다.
    (``save_roi``는 DB 세션이 필요하므로, 그 안에서 실제로 쓰는 정리
    로직 ``_clean_arrows``를 직접 확인한다.)"""
    cleaned = C._clean_arrows([[[10, 10], [10, 100]]], w=640, h=360)
    assert cleaned == [[[10, 10], [10, 100]]]


def test_점이_2개가_아닌_항목은_저장에서_빠진다():
    cleaned = C._clean_arrows(
        [[[10, 10], [10, 100]], [[5, 5], [5, 5], [5, 5]]], w=640, h=360)
    assert cleaned == [[[10, 10], [10, 100]]]


def test_길이가_0인_항목은_저장에서_빠진다():
    cleaned = C._clean_arrows(
        [[[10, 10], [10, 100]], [[5, 5], [5, 5]]], w=640, h=360)
    assert cleaned == [[[10, 10], [10, 100]]]


def test_화면_밖_좌표는_잘린다():
    cleaned = C._clean_arrows([[[-10, -10], [700, 400]]], w=640, h=360)
    assert cleaned == [[[0, 0], [639, 359]]]


# --- roi_audit -----------------------------------------------------------------


def test_화살표는_진단에서_사라지지_않는다():
    """★ 회귀 방지 — 폴리곤 전용 len(p)>=3 필터에 걸려 조용히 사라지면
    안 된다. 정상 화살표는 지적 없이 통과해야 한다."""
    a = RA.audit_shapes("CAM", "traffic", {
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 500], [0, 500]]],
                  ARROW_KEY: [[[100, 100], [100, 500]]]}})
    assert a.findings == []


def test_길이_0_화살표는_진단이_잡는다():
    a = RA.audit_shapes("CAM", "traffic", {
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 500], [0, 500]]],
                  ARROW_KEY: [[[100, 100], [100, 100]]]}})
    assert "DEGENERATE" in _codes(a)


def test_화면_밖_화살표는_진단이_잡는다():
    a = RA.audit_shapes("CAM", "traffic", {
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 500], [0, 500]]],
                  ARROW_KEY: [[[2000, 2000], [3000, 3000]]]}})
    assert "OUT_OF_FRAME" in _codes(a)


def test_화살표가_없어도_필수_구역이_아니라_지적하지_않는다():
    """flow_arrows 는 REQUIRED_SHAPE 에 없다 — 안 그렸다고 MISSING을
    내면 안 된다(congestion_roi 는 그려져 있다고 가정)."""
    a = RA.audit_shapes("CAM", "traffic", {
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 500], [0, 500]]]}})
    assert a.findings == []
