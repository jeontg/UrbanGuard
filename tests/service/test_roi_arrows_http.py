"""통행 방향 화살표(`flow_arrows`) ROI 저장 — HTTP 경로 (Phase 4, 2026-08-26).

``core/roi_audit.py``·``core/cameras.py`` 단위 시험(``test_roi_arrows.py``)은
로직을 확인하지만, 실제로 화면이 쓰는 경로(POST → save_roi → DB → 재조회)를
왕복하지는 않는다. 여기서 그 전체 경로를 확인한다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi
from tot_dashboard.service.main import app

CAM_ID = "ARROW-HTTP-CAM"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(CameraRoi).where(CameraRoi.camera_id == CAM_ID))
        db.execute(sa_delete(CameraDomain).where(CameraDomain.camera_id == CAM_ID))
        db.execute(sa_delete(Camera).where(Camera.id == CAM_ID))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def camera(db_schema):
    _purge()
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": CAM_ID, "name": "화살표시험지점",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/a.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        C.set_domains(db, cam, {"traffic": {"enabled": True, "continuous": False}})
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def test_화살표를_저장하면_그대로_다시_읽힌다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/roi/traffic", json={
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 500], [0, 500]]],
                  "flow_arrows": [[[100, 100], [100, 500]],
                                  [[600, 500], [600, 100]]]}})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True, body

    db = get_session()
    try:
        row = db.scalar(select(CameraRoi).where(
            CameraRoi.camera_id == CAM_ID, CameraRoi.domain == "traffic"))
        assert row is not None
        assert row.shapes["flow_arrows"] == [
            [[100, 100], [100, 500]], [[600, 500], [600, 100]]]
    finally:
        db.close()


def test_점이_2개가_아닌_화살표는_저장이_거부된다(client):
    res = client.post(f"/settings/cameras/{CAM_ID}/roi/traffic", json={
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"flow_arrows": [[[100, 100], [200, 200], [300, 300]]]}})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert any("2개" in e for e in body["errors"])


def test_화살표_없이도_다른_ROI는_정상_저장된다(client):
    """flow_arrows 는 필수가 아니다 — 안 그려도 나머지 저장을 막으면 안 된다."""
    res = client.post(f"/settings/cameras/{CAM_ID}/roi/traffic", json={
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 500], [0, 500]]]}})
    assert res.status_code == 200
    assert res.json()["ok"] is True
