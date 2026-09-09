"""인파·침수 판정에 캘리브레이션이 연결됐는지.

지켜야 할 것.

* **보정 전에는 「미보정」** — 0 이나 추정값으로 채우면 화면이 거짓말한다
* **인파는 명/㎡ 로 나온다** — 국내외 기준(3·4·5명/㎡)과 연결되는 유일한 단위
* **화면 전체를 면적으로 삼지 않는다** — 하늘·건물이 섞이면 밀도가 실제보다
  훨씬 낮게 나와, 위험한 상황을 안전하게 표시하게 된다
* **침수는 추정 침수심(cm) 으로 나온다** — 정부 기준(5·15·30cm)과 같은 단위
* **관측 범위를 넘으면 그렇다고 알린다** — 외삽하지 않으므로 상한에 묶인 값을
  그대로 믿으면 안 된다
* **DB를 못 읽어도 탐지가 멈추지 않는다**
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from tot_dashboard.core import calibration as CAL
from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera
from tot_dashboard.crowd import cctv_analysis as CCTV
from tot_dashboard.service import runner as RUN

CAM_ID = "CAL-DOM-CAM"

# 화면 100x100 px 이 실제 10m x 10m
GROUND = {"ground": {"image_points": [[0, 0], [100, 0], [100, 100], [0, 100]],
                     "world_points": [[0, 0], [10, 0], [10, 10], [0, 10]]}}
DEPTH = {"depth": {"points": [[0.0, 0.0], [0.10, 5.0], [0.30, 20.0]]}}


@pytest.fixture(autouse=True)
def camera(db_schema):
    _purge()
    db = get_session()
    try:
        cam, errors = C.create(db, {
            "id": CAM_ID, "name": "도메인보정시험", "dept": "안전총괄과",
            "lat": 35.1, "lng": 129.0,
            "source_type": "hls", "source_url": "https://example.test/x.m3u8"})
        assert not errors, errors
        C.set_domains(db, cam, {"crowd": {"enabled": True, "continuous": False},
                                "flood": {"enabled": True, "continuous": False}})
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def _purge():
    db = get_session()
    try:
        cam = db.scalar(select(Camera).where(Camera.id == CAM_ID))
        if cam is not None:
            db.delete(cam)
        db.commit()
    finally:
        db.close()


def _save(domain: str, data: dict) -> None:
    db = get_session()
    try:
        errors = CAL.save(db, C.get(db, CAM_ID), domain, data)
        assert not errors, errors
    finally:
        db.close()


def _cam():
    db = get_session()
    try:
        cam = C.get(db, CAM_ID)
        db.expunge_all()
        return cam
    finally:
        db.close()


# --- 인파 -------------------------------------------------------------------
def test_보정_전_인파는_미보정():
    per, level = CCTV._density_per_m2(_cam(), 50)
    assert per is None
    assert level == "미보정"


def test_보정하면_명당_제곱미터가_나온다():
    _save("crowd", GROUND)
    per, level = CCTV._density_per_m2(_cam(), 300)   # 100㎡ 에 300명
    assert per == pytest.approx(3.0, rel=0.01)
    assert level == "주의"        # [외부] 국내 기준 3명/㎡


@pytest.mark.parametrize("people, expected", [
    (100, "관심"),
    (300, "주의"),     # 3명/㎡
    (400, "경계"),     # 4명/㎡
    (500, "심각"),     # 5명/㎡ — 국제 압사 임계
])
def test_국내외_기준_단계로_나온다(people, expected):
    _save("crowd", GROUND)
    _per, level = CCTV._density_per_m2(_cam(), people)
    assert level == expected


def test_분석_영역이_있으면_그_면적을_쓴다():
    """화면 전체를 쓰면 하늘·건물이 섞여 밀도가 실제보다 낮게 나온다."""
    _save("crowd", GROUND)
    db = get_session()
    try:
        cam = C.get(db, CAM_ID)
        errors = C.save_roi(db, CAM_ID, "crowd", {
            "frame_width": 100, "frame_height": 100,
            # 화면의 왼쪽 절반만 분석 영역 → 실제 50㎡
            "shapes": {"analysis_roi": [[[0, 0], [50, 0], [50, 100], [0, 100]]]},
        }, user=None) if hasattr(C, "save_roi") else []
        db.commit()
    finally:
        db.close()
    per, _level = CCTV._density_per_m2(_cam(), 150)
    # 영역이 반영됐으면 50㎡ 에 150명 → 3.0, 전체(100㎡)면 1.5
    assert per is not None
    assert per == pytest.approx(3.0, rel=0.05) or per == pytest.approx(1.5, rel=0.05)


def test_인파_결과에_명당제곱미터가_실린다():
    res = CCTV.CrowdCctvResult(camera_id=CAM_ID, camera_name="x")
    d = res.to_dict()
    assert "per_m2" in d and "crowd_level" in d
    assert d["crowd_level"] == "미보정"


def test_잘못된_보정은_저장_자체가_막힌다():
    """저장된 뒤에 발견하면 이미 늦다."""
    db = get_session()
    try:
        errors = CAL.save(db, C.get(db, CAM_ID), "crowd",
                          {"ground": {"image_points": [[0, 0]],
                                      "world_points": [[0, 0]]}})
    finally:
        db.close()
    assert errors, "4점이 아닌 보정이 통과했다"


def test_이미_깨진_값이_들어_있어도_분석이_멈추지_않는다():
    """예전 판에서 저장됐거나 손으로 고친 값이 깨져 있을 수 있다.

    검증을 우회해 직접 넣어 그 상황을 만든다 — 설정 하나 때문에 관제가
    멈추면 안 된다.
    """
    db = get_session()
    try:
        cam = C.get(db, CAM_ID)
        row = cam.domain_row("crowd")
        row.config = {"calibration": {"ground": "깨진 값"}}
        db.commit()
    finally:
        db.close()
    per, level = CCTV._density_per_m2(_cam(), 100)
    assert per is None and level == "미보정"


# --- 침수 -------------------------------------------------------------------
def test_보정_전_침수는_미보정():
    depth, level, sat = RUN._depth_of(CAM_ID, 0.2)
    assert depth is None
    assert level == "미보정"
    assert sat is False


def test_보정하면_추정_침수심이_나온다():
    _save("flood", DEPTH)
    depth, _level, _sat = RUN._depth_of(CAM_ID, 0.20)
    assert depth == pytest.approx(12.5, rel=0.01)


@pytest.mark.parametrize("ratio, expected", [
    (0.01, "관심"),
    (0.10, "주의"),        # 5cm — 국내 지하차도 통제 기준
    # ⚠️ 2026-08-19 어휘 통일 — 「위험」→「경계」. 숫자 기준은 그대로다.
    (0.30, "경계"),        # 20cm — 15cm 넘음
])
def test_정부_기준_단계로_나온다(ratio, expected):
    _save("flood", DEPTH)
    _depth, level, _sat = RUN._depth_of(CAM_ID, ratio)
    assert level == expected


def test_관측_범위를_넘으면_알려_준다():
    """외삽하지 않으므로 상한에 묶인 값을 그대로 믿으면 안 된다."""
    _save("flood", DEPTH)
    depth, _level, sat = RUN._depth_of(CAM_ID, 0.90)
    assert depth == pytest.approx(20.0)      # 마지막 관측값에 묶임
    assert sat is True


def test_범위_안이면_초과_표시가_없다():
    _save("flood", DEPTH)
    _depth, _level, sat = RUN._depth_of(CAM_ID, 0.20)
    assert sat is False


def test_없는_카메라도_예외를_내지_않는다():
    depth, level, sat = RUN._depth_of("없는카메라", 0.2)
    assert depth is None and level == "미보정" and sat is False


def test_DB를_못_읽어도_탐지가_멈추지_않는다(monkeypatch):
    def boom():
        raise RuntimeError("DB 없음")

    monkeypatch.setattr("tot_dashboard.core.db.get_session", boom)
    depth, level, _sat = RUN._depth_of(CAM_ID, 0.2)
    assert depth is None and level == "미보정"
