"""탐지 지정을 바꿔도 ROI 는 남는다 (S-80 → S-81).

**신고받은 증상.** CCTV 를 등록하고 ROI 를 설정한 뒤 「탐지 지정」을 바꾸면
ROI 가 사라진다.

**실제로 확인한 사실.** ROI 데이터(``camera_rois``)는 지워지지 않는다.
``camera_domains`` 와 **별도 테이블**이고 탐지 지정은 그쪽만 건드린다.
사라진 것은 **화면이었다** — ROI 편집기가 「사용」으로 지정된 도메인만 탭에
띄우고, 목록의 ROI 열도 그때만 배지를 그렸다.

보이지 않으면 운영자는 다시 그린다. **그것이 진짜 손실이다.** 그래서 데이터가
남아 있다는 사실을 화면이 말하게 고쳤다.

지켜야 할 것.

* 탐지 지정을 꺼도 ROI 행은 남는다
* 다시 켜면 **그대로** 쓰인다 (다시 그릴 필요 없음)
* 꺼진 동안에도 편집기 탭과 목록에서 **보인다**
* 끄는 순간 「보관됩니다」라고 알려 준다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi
from tot_dashboard.service.main import app

CAM_ID = "ROITEST-1"

FLOOD_ROI = {
    "frame_width": 640, "frame_height": 360,
    "shapes": {"road_roi": [[[0, 0], [100, 0], [100, 100], [0, 100]]]},
}


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(CameraRoi))
        s.execute(sa_delete(CameraDomain))
        s.execute(sa_delete(Camera))
        s.commit()
    finally:
        s.close()


def _seed(db, *, flood=True, crowd=False):
    """카메라 하나 + 침수 ROI 를 심는다."""
    cam, errs = C.create(db, {"id": CAM_ID, "name": "ROI시험지점",
                              "source_type": "hls",
                              "source_url": "https://example.test/a.m3u8",
                              "lat": "35.1", "lng": "129.0"})
    assert not errs, errs
    C.set_domains(db, cam, {"flood": {"enabled": flood, "continuous": flood},
                            "crowd": {"enabled": crowd},
                            "road": {"enabled": False}})
    db.commit()
    _, errs = C.save_roi(db, CAM_ID, "flood", FLOOD_ROI, None)
    assert not errs, errs
    db.commit()
    return cam


# --- 데이터 -----------------------------------------------------------------

def test_탐지지정을_꺼도_ROI_행이_남는다():
    db = get_session()
    try:
        _seed(db)
        assert len(db.scalars(select(CameraRoi)).all()) == 1

        cam = C.get(db, CAM_ID)
        C.set_domains(db, cam, {"flood": {"enabled": False},
                                "crowd": {"enabled": True},
                                "road": {"enabled": False}})
        db.commit()
        db.expire_all()

        rows = db.scalars(select(CameraRoi)).all()
        assert len(rows) == 1
        assert rows[0].domain == "flood"
        assert rows[0].shapes.get("road_roi")
    finally:
        db.rollback()
        db.close()


def test_다시_켜면_그대로_쓰인다():
    """다시 그릴 필요가 없어야 한다."""
    db = get_session()
    try:
        _seed(db)
        before = C.roi_of(db, CAM_ID, "flood")["shapes"]["road_roi"]

        cam = C.get(db, CAM_ID)
        C.set_domains(db, cam, {"flood": {"enabled": False}})
        db.commit()
        cam = C.get(db, CAM_ID)
        C.set_domains(db, cam, {"flood": {"enabled": True}})
        db.commit()
        db.expire_all()

        assert C.roi_of(db, CAM_ID, "flood")["shapes"]["road_roi"] == before
    finally:
        db.rollback()
        db.close()


def test_has_roi_는_빈_영역을_설정됨으로_보지_않는다():
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": "ROITEST-2", "name": "빈지점",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/b.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs
        db.commit()
        assert C.has_roi(cam, "flood") is False
    finally:
        db.rollback()
        db.close()


# --- 화면 -------------------------------------------------------------------

def test_지정이_꺼져도_편집기_탭에_남는다(client):
    db = get_session()
    try:
        _seed(db)
        cam = C.get(db, CAM_ID)
        C.set_domains(db, cam, {"flood": {"enabled": False},
                                "crowd": {"enabled": True},
                                "road": {"enabled": False}})
        db.commit()
    finally:
        db.close()

    r = client.get(f"/settings/cameras/{CAM_ID}/roi")
    assert r.status_code == 200
    # 침수 탭이 「미지정」 표시와 함께 남아 있어야 한다.
    assert "미지정" in r.text
    assert "domain=flood" in r.text


def test_꺼진_도메인을_열면_보관중이라고_알려_준다(client):
    db = get_session()
    try:
        _seed(db)
        cam = C.get(db, CAM_ID)
        C.set_domains(db, cam, {"flood": {"enabled": False},
                                "crowd": {"enabled": True},
                                "road": {"enabled": False}})
        db.commit()
    finally:
        db.close()

    r = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=flood")
    assert r.status_code == 200
    assert "지워지지 않고 보관" in r.text
    # 그리고 그려 둔 좌표가 실제로 화면에 실려야 한다.
    assert "road_roi" in r.text


def test_기본_탭은_쓰고_있는_도메인이_먼저다(client):
    """보관 중인 탭이 먼저 열려 「지금 안 쓰는 영역」을 만지게 하면 안 된다."""
    db = get_session()
    try:
        _seed(db, flood=False, crowd=True)
        db.commit()
    finally:
        db.close()

    r = client.get(f"/settings/cameras/{CAM_ID}/roi")
    assert r.status_code == 200
    # 인파(사용)가 기본 선택이어야 한다 — 침수는 보관 탭.
    assert 'var DOMAIN = "crowd"' in r.text


def test_목록에_보관_배지가_뜬다(client):
    db = get_session()
    try:
        _seed(db)
        cam = C.get(db, CAM_ID)
        C.set_domains(db, cam, {"flood": {"enabled": False},
                                "crowd": {"enabled": True},
                                "road": {"enabled": False}})
        db.commit()
    finally:
        db.close()

    r = client.get("/settings/cameras")
    assert r.status_code == 200
    assert "보관" in r.text


def test_탐지지정을_끄면_보관된다고_안내한다(client):
    db = get_session()
    try:
        _seed(db)
    finally:
        db.close()

    # 침수를 끄고 인파를 켠다.
    r = client.post(f"/settings/cameras/{CAM_ID}/domains",
                    data={"crowd_enabled": "1"})
    assert r.status_code == 200
    assert "지워지지 않고 보관됩니다" in r.text

    # 안내만 하고 실제로도 안 지워야 한다.
    db = get_session()
    try:
        assert C.roi_of(db, CAM_ID, "flood")["shapes"].get("road_roi")
    finally:
        db.close()
