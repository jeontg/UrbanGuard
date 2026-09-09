"""캘리브레이션이 실제 판정에 연결되는지 (S-80 입력 · 노면 건/100m).

지켜야 할 것.

* **보정 전에는 「미보정」** — 0 이나 추정값으로 채우면 화면이 거짓말한다
* **보정하면 건/100m 가 나온다** — 개수만으로는 긴 구간이 항상 불리하다
* **잘못된 입력은 저장되지 않는다** — 저장된 뒤에 발견하면 이미 늦다
* **DB를 못 읽어도 관제가 멈추지 않는다**
* **저장은 감사 로그에 남는다**
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tot_dashboard.core import calibration as CAL
from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog, Camera
from tot_dashboard.road import results as RR
from tot_dashboard.service.main import app

CAM_ID = "CAL-TEST-CAM"


@pytest.fixture(scope="module")
def anon_client():
    # 수명주기를 켜지 않는다 — runner 스레드는 프로세스당 한 번만 start 가능.
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def camera(db_schema):
    _purge()
    db = get_session()
    try:
        cam, errors = C.create(db, {
            "id": CAM_ID, "name": "보정시험지점", "dept": "도로과",
            "lat": 35.1, "lng": 129.0,
            "source_type": "hls", "source_url": "https://example.test/x.m3u8",
        })
        assert not errors, errors
        C.set_domains(db, cam, {"road": {"enabled": True, "continuous": False}})
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def _purge():
    db = get_session()
    try:
        db.execute(delete(AuditLog).where(AuditLog.target.like(f"%{CAM_ID}%")))
        cam = db.scalar(select(Camera).where(Camera.id == CAM_ID))
        if cam is not None:
            db.delete(cam)
        db.commit()
    finally:
        db.close()
    RR.clear()


def _cal():
    db = get_session()
    try:
        return CAL.of(C.get(db, CAM_ID), "road")
    finally:
        db.close()


# --- 저장 -------------------------------------------------------------------
def test_구간_길이를_저장한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "road", "section_length_m": "200"})
    assert res.status_code == 200
    cal = _cal()
    assert cal.section is not None
    assert cal.section.length_m == 200.0


def test_저장은_감사_로그에_남는다(client):
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "road", "section_length_m": "150"})
    db = get_session()
    try:
        rows = db.scalars(select(AuditLog).where(
            AuditLog.target.like("캘리브레이션%"))).all()
        assert rows, "캘리브레이션 저장이 감사 로그에 남지 않았다"
    finally:
        db.close()


def test_잘못된_구간_길이는_저장되지_않는다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "road", "section_length_m": "-5"})
    assert res.status_code == 400
    assert _cal().section is None


def test_비상식적으로_긴_구간은_거부한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "road", "section_length_m": "99999"})
    assert res.status_code == 400
    assert "단위" in res.text


def test_숫자가_아니면_거부한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "road", "section_length_m": "이백미터"})
    assert res.status_code == 400
    assert _cal().section is None


def test_JSON이_깨지면_거부한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "flood", "depth_points": "[[0,0],[0.1,"})
    assert res.status_code == 400
    assert "JSON" in res.text


def test_알_수_없는_도메인은_거부한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "weather", "section_length_m": "100"})
    assert res.status_code == 400


def test_침수심_대응표를_저장한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration",
                      data={"domain": "flood",
                            "depth_points": "[[0,0],[0.1,5],[0.3,20]]"})
    assert res.status_code == 200
    db = get_session()
    try:
        cal = CAL.of(C.get(db, CAM_ID), "flood")
    finally:
        db.close()
    assert cal.depth is not None
    assert CAL.flood_depth_cm(cal, 0.2) == pytest.approx(12.5, rel=0.01)


def test_지면_평면을_저장한다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration", data={
        "domain": "crowd",
        "ground_image_points": "[[0,0],[100,0],[100,100],[0,100]]",
        "ground_world_points": "[[0,0],[10,0],[10,10],[0,10]]"})
    assert res.status_code == 200
    db = get_session()
    try:
        cal = CAL.of(C.get(db, CAM_ID), "crowd")
    finally:
        db.close()
    assert cal.ground is not None
    area = cal.ground.area_m2([[0, 0], [100, 0], [100, 100], [0, 100]])
    assert area == pytest.approx(100.0, rel=0.01)


def test_교통_지면_평면을_저장한다(client):
    """Phase 2 (2026-08-26, docs/202608260842/ §1 결함 시정) — 교통도 인파와
    같은 지면 평면 방식으로 km/h 를 실측한다. 예전에는 이 폼 자체가 없어
    화면의 km/h 가 항상 가짜 고정계수(mpp=0.06)였다."""
    res = client.post(f"/settings/cameras/{CAM_ID}/calibration", data={
        "domain": "traffic",
        "ground_image_points": "[[0,0],[100,0],[100,100],[0,100]]",
        "ground_world_points": "[[0,0],[10,0],[10,10],[0,10]]"})
    assert res.status_code == 200
    # ★ 성공 문구도 도메인에 맞아야 한다 — 인파용 "지면 평면(명/㎡ 산출
    # 가능)" 문구가 그대로 뜨면 교통 관리자가 혼란스럽다(페이지 상단
    # 범례에는 다른 도메인 안내로 "명/㎡"가 계속 나오므로, 알림 문구
    # 자체만 좁혀서 확인한다).
    assert "저장했습니다 — 지면 평면(km/h 실측 가능)" in res.text
    assert "지면 평면(명/㎡ 산출 가능)" not in res.text
    db = get_session()
    try:
        cal = CAL.of(C.get(db, CAM_ID), "traffic")
    finally:
        db.close()
    assert cal.ground is not None
    assert CAL.traffic_speed_kmh(cal, [0, 0], [100, 0], 1.0) == pytest.approx(36.0, rel=0.01)


def test_교통_보정_전에는_다른_도메인_보정이_있어도_미보정이다(client):
    """도메인마다 따로 저장된다 — road/crowd 보정이 있어도 traffic 은
    독립적으로 미보정이어야 한다."""
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "road", "section_length_m": "200"})
    db = get_session()
    try:
        cal = CAL.of(C.get(db, CAM_ID), "traffic")
    finally:
        db.close()
    assert cal.ground is None


def test_다른_도메인_보정을_덮어쓰지_않는다(client):
    """도메인마다 따로 저장되어야 한다."""
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "road", "section_length_m": "200"})
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "flood", "depth_points": "[[0,0],[0.2,10]]"})
    db = get_session()
    try:
        cam = C.get(db, CAM_ID)
        assert CAL.of(cam, "road").section is not None
        assert CAL.of(cam, "flood").depth is not None
    finally:
        db.close()


# --- 노면 판정 연결 ----------------------------------------------------------
def _result(n_defects: int) -> dict:
    return {"defects": [{"type": "pothole", "confidence": 0.9,
                         "box": [0, 0, 10, 10], "frame": 0}] * n_defects,
            "frames_analyzed": 5, "grade": 3, "grade_label": "주의"}


def test_보정_전에는_미보정(client):
    RR.record(CAM_ID, _result(3), source="manual")
    latest = RR.get(CAM_ID)
    assert latest["per_100m"] is None
    assert latest["density_level"] == "미보정"


def test_보정하면_건_100미터가_나온다(client):
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "road", "section_length_m": "200"})
    RR.record(CAM_ID, _result(6), source="manual")
    latest = RR.get(CAM_ID)
    assert latest["per_100m"] == pytest.approx(3.0)
    assert latest["density_level"] == "보수 필요"


def test_긴_구간이_불리하지_않다(client):
    """개수만 세면 긴 구간이 항상 나빠 보인다. 그래서 구간 단위로 바꿨다."""
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "road", "section_length_m": "1000"})
    RR.record(CAM_ID, _result(5), source="manual")
    assert RR.get(CAM_ID)["per_100m"] == pytest.approx(0.5)
    assert RR.get(CAM_ID)["density_level"] == "양호"


def test_이력에도_건_100미터가_남는다(client):
    client.post(f"/settings/cameras/{CAM_ID}/calibration",
                data={"domain": "road", "section_length_m": "100"})
    RR.record(CAM_ID, _result(2), source="continuous")
    hist = RR.history(CAM_ID)
    assert hist and hist[-1]["per_100m"] == pytest.approx(2.0)


def test_DB를_못_읽어도_기록은_된다(monkeypatch):
    """관제가 설정 조회 실패로 멈추면 안 된다."""
    def boom():
        raise RuntimeError("DB 없음")

    monkeypatch.setattr("tot_dashboard.core.db.get_session", boom)
    RR.record(CAM_ID, _result(1), source="manual")
    latest = RR.get(CAM_ID)
    assert latest is not None
    assert latest["density_level"] == "미보정"
