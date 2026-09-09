"""관계 모델의 화면·라우트 (S-80 · S-03 · S-01).

지켜야 할 것.

* **관계가 없으면 화면이 그냥 안 보인다** — 빈 카드로 자리를 차지하지 않는다
* ⚠️ **`/settings/cameras/links/...` 가 `{camera_id}` 로 안 잡힌다** —
  지역 해지 라우트에서 실제로 겪었던 문제라 시험으로 못박는다
* **미리보기는 아무것도 쓰지 않는다**
* **권한 없는 사용자는 못 만진다**
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.service.main import app
from tot_dashboard.core import relations as R
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraLink, Event

PFX = "TEST-SCR-"


def _purge(db):
    db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
    db.execute(sa_delete(CameraLink).where(
        CameraLink.from_camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
    db.commit()


@pytest.fixture
def db(db_schema, seeded_users):
    s = get_session()
    _purge(s)
    V.seed_builtin(s)
    s.commit()
    yield s
    _purge(s)
    s.close()


@pytest.fixture
def admin_client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


@pytest.fixture
def opr_client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["opr"])
    yield c
    c.cookies.clear()


@pytest.fixture
def cams(db):
    db.add(Camera(id=f"{PFX}UP", name="시험상류", lat=35.1010, lng=129.0300))
    db.add(Camera(id=f"{PFX}DOWN", name="시험하류", lat=35.1000, lng=129.0300))
    db.commit()
    return db


# --- S-80 인접 자동 생성 ----------------------------------------------------


def test_links_build_route_is_not_shadowed(admin_client, cams):
    """⚠️ `links` 가 camera_id 로 잡히면 「카메라를 찾을 수 없습니다」가 된다."""
    r = admin_client.post("/settings/cameras/links/build",
                          data={"radius_m": "500", "preview": "1"})
    assert r.status_code == 200
    assert "카메라를 찾을 수 없" not in r.text


def test_preview_writes_nothing(admin_client, cams):
    admin_client.post("/settings/cameras/links/build",
                      data={"radius_m": "500", "preview": "1"})
    cams.expire_all()
    assert R.neighbors(cams, f"{PFX}UP") == []


def test_build_creates_links(admin_client, cams):
    r = admin_client.post("/settings/cameras/links/build",
                          data={"radius_m": "500"})
    assert r.status_code == 200
    cams.expire_all()
    assert R.neighbors(cams, f"{PFX}UP") == [(f"{PFX}DOWN", 1)]


def test_build_rejects_bad_radius(admin_client, cams):
    r = admin_client.post("/settings/cameras/links/build",
                          data={"radius_m": "10"})
    assert r.status_code == 400
    assert "50~5000" in r.text


def test_build_requires_permission(opr_client, cams):
    r = opr_client.post("/settings/cameras/links/build",
                        data={"radius_m": "500"})
    assert r.status_code == 403


# --- S-80 상·하류 지정 ------------------------------------------------------


def test_flow_set_and_clear(admin_client, cams):
    r = admin_client.post("/settings/cameras/links/flow",
                          data={"upper_id": f"{PFX}UP",
                                "lower_id": f"{PFX}DOWN"})
    assert r.status_code == 200
    cams.expire_all()
    assert R.downstream_of(cams, f"{PFX}UP") == [(f"{PFX}DOWN", 1)]

    r = admin_client.post("/settings/cameras/links/flow",
                          data={"upper_id": f"{PFX}UP",
                                "lower_id": f"{PFX}DOWN", "remove": "1"})
    assert r.status_code == 200
    cams.expire_all()
    assert R.downstream_of(cams, f"{PFX}UP") == []


def test_flow_requires_both_points(admin_client, cams):
    r = admin_client.post("/settings/cameras/links/flow",
                          data={"upper_id": f"{PFX}UP", "lower_id": ""})
    assert r.status_code == 400


def test_flow_requires_permission(opr_client, cams):
    r = opr_client.post("/settings/cameras/links/flow",
                        data={"upper_id": f"{PFX}UP",
                              "lower_id": f"{PFX}DOWN"})
    assert r.status_code == 403


# --- S-80 방향각 ------------------------------------------------------------


def test_bearing_can_be_saved_and_left_blank(admin_client, cams):
    """방향각은 **비워 둘 수 있어야** 한다 — 모르면 비우는 것이 정답이다."""
    base = {"name": "시험상류", "lat": "35.1010", "lng": "129.0300",
            "source_type": "hls", "source_url": "https://x/y.m3u8"}
    r = admin_client.post(f"/settings/cameras/{PFX}UP/update",
                          data={**base, "bearing_deg": "90", "purpose": "disaster"})
    assert r.status_code == 200
    cams.expire_all()
    cam = cams.get(Camera, f"{PFX}UP")
    assert cam.bearing_deg == 90 and cam.purpose == "disaster"

    r = admin_client.post(f"/settings/cameras/{PFX}UP/update",
                          data={**base, "bearing_deg": "", "purpose": ""})
    assert r.status_code == 200
    cams.expire_all()
    cam = cams.get(Camera, f"{PFX}UP")
    assert cam.bearing_deg is None and cam.purpose == ""


def test_bearing_out_of_range_is_rejected(admin_client, cams):
    r = admin_client.post(f"/settings/cameras/{PFX}UP/update", data={
        "name": "시험상류", "lat": "35.1010", "lng": "129.0300",
        "source_type": "hls", "source_url": "https://x/y.m3u8",
        "bearing_deg": "400"})
    assert r.status_code == 400
    assert "방위각" in r.text


# --- S-03 주변 지점 ---------------------------------------------------------


def test_event_detail_hides_nearby_when_no_links(admin_client, cams):
    """관계가 없으면 카드 자체가 안 보인다."""
    ev = Event(domain="flood", block_id=f"{PFX}UP", place_name="시험상류",
               event_type="침수", level="경계", status="open")
    cams.add(ev)
    cams.commit()
    r = admin_client.get(f"/events/{ev.id}")
    assert r.status_code == 200
    assert "주변 지점" not in r.text


def test_event_detail_shows_downstream(admin_client, cams):
    R.set_flow(cams, f"{PFX}UP", f"{PFX}DOWN")
    cams.commit()
    ev = Event(domain="flood", block_id=f"{PFX}UP", place_name="시험상류",
               event_type="침수", level="경계", status="open")
    cams.add(ev)
    cams.commit()

    r = admin_client.get(f"/events/{ev.id}")
    assert r.status_code == 200
    assert "주변 지점" in r.text
    assert "시험하류" in r.text
    assert "하류" in r.text


# --- S-01 선행 경고 ---------------------------------------------------------


def test_home_hides_warning_without_flow(admin_client, cams):
    r = admin_client.get("/")
    assert r.status_code == 200
    assert "선행 경고" not in r.text


def test_home_shows_warning(admin_client, cams):
    R.set_flow(cams, f"{PFX}UP", f"{PFX}DOWN")
    cams.commit()
    cams.add(Event(domain="flood", block_id=f"{PFX}UP", place_name="시험상류",
                   event_type="침수", level="심각", status="open"))
    cams.commit()

    r = admin_client.get("/")
    assert r.status_code == 200
    assert "선행 경고" in r.text
    assert "시험하류" in r.text
