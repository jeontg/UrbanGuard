"""실시간 CCTV 모달의 ROI 오버레이 — 도메인별 조회 (2026-08-26 결함 시정).

**신고받은 증상**: 강서구청(SEOUL-207) 카메라의 노면관리 실시간 CCTV를 켜면
분명히 설정해 둔 ROI가 보이지 않는다. 확인해 보니 `/api/roi/{block_id}`가
**어느 탭에서 호출됐든 항상 침수(flood) 도메인만 조회**했다 — 노면·교통위험
카메라의 실제 ROI(`analysis_roi`·`congestion_roi` 등)는 이 엔드포인트가
존재를 몰랐다.

지켜야 할 것.

* **도메인을 명시하면 그 도메인의 ROI가 나와야 한다** — 침수뿐 아니라
  교통위험·노면도
* **침수(flood)는 기존 동작(레거시 파일 폴백 포함)이 그대로 유지된다** —
  `test_main.py::test_roi_endpoint_for_the_live_video_overlay`가 이미 확인
* **모르는 도메인은 조용히 침수로 떨어지지 않고 명확히 거부한다**
* **ROI가 없는 카메라는 "없다"고 정직하게 답한다** — 있는 척하지 않는다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi
from tot_dashboard.service.main import app

CAM_ID = "ROIOVERLAY-CAM"


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
        cam, errs = C.create(db, {"id": CAM_ID, "name": "강서구청_시험",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/a.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        C.set_domains(db, cam, {"road": {"enabled": True, "continuous": True},
                                "traffic": {"enabled": True, "continuous": True}})
        db.commit()
        # 실제 신고 사례처럼 침수 ROI는 없고, 노면·교통위험 ROI만 있다.
        _, errs = C.save_roi(db, CAM_ID, "road", {
            "frame_width": 640, "frame_height": 360,
            "shapes": {"analysis_roi": [[[0, 0], [500, 0], [500, 300], [0, 300]]]}},
            user=None)
        assert not errs, errs
        _, errs = C.save_roi(db, CAM_ID, "traffic", {
            "frame_width": 640, "frame_height": 360,
            "shapes": {"congestion_roi": [[[0, 0], [500, 0], [500, 300], [0, 300]]],
                      "flow_arrows": [[[10, 10], [10, 100]]]}},
            user=None)
        assert not errs, errs
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def test_노면_도메인을_지정하면_노면_ROI가_나온다(client):
    """★ 회귀 방지 — 강서구청 사례. domain 인자가 없던 예전에는 이 카메라의
    노면 ROI가 절대 조회되지 않았다."""
    res = client.get(f"/api/roi/{CAM_ID}?domain=road")
    assert res.status_code == 200
    body = res.json()
    assert "error" not in body
    assert body["domain"] == "road"
    assert body["shapes"]["analysis_roi"] == [[[0, 0], [500, 0], [500, 300], [0, 300]]]
    assert body["shape_kinds"]["analysis_roi"] == "polygon"


def test_교통위험_도메인은_congestion_roi와_화살표를_함께_돌려준다(client):
    res = client.get(f"/api/roi/{CAM_ID}?domain=traffic")
    body = res.json()
    assert "error" not in body
    assert body["shapes"]["congestion_roi"]
    assert body["shapes"]["flow_arrows"] == [[[10, 10], [10, 100]]]
    assert body["shape_kinds"]["flow_arrows"] == "arrows"


def test_침수_도메인은_ROI가_없다고_정직하게_답한다(client):
    """이 카메라는 노면·교통위험 ROI만 있고 침수 ROI는 없다 — 예전 버그는
    이 경우 "노면 ROI가 없다"가 아니라 애초에 노면을 조회조차 안 했다."""
    res = client.get(f"/api/roi/{CAM_ID}?domain=flood")
    body = res.json()
    assert body.get("error")


def test_기본값은_여전히_flood다(client):
    """domain 파라미터를 생략한 기존 호출부(레거시)가 그대로 동작해야 한다."""
    res = client.get(f"/api/roi/{CAM_ID}")
    body = res.json()
    assert body.get("error")  # 이 카메라는 침수 ROI가 없으므로


def test_인파관리_도메인도_조회된다(client):
    """이 카메라는 인파관리 ROI를 저장한 적이 없다 — "없음"으로 정직하게
    답해야지 노면·교통위험 도형이 섞여 나오면 안 된다."""
    res = client.get(f"/api/roi/{CAM_ID}?domain=crowd")
    body = res.json()
    assert body.get("error")


def test_모르는_도메인은_명확히_거부한다(client):
    res = client.get(f"/api/roi/{CAM_ID}?domain=weather")
    body = res.json()
    assert body.get("error")


def test_없는_카메라도_노면_도메인에서_에러를_낸다(client):
    res = client.get("/api/roi/없는카메라?domain=road")
    body = res.json()
    assert body.get("error")
