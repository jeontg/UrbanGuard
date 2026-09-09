"""통행 방향 화살표 자동 생성 API — traffic-service (API 게이트웨이 Phase 4).

⚠️ 2026-08-31 신설 — 예전에는 이 API가 platform-shell
(``/settings/cameras/{id}/roi/traffic/flow/propose``)에 있었다
(``test_flow_propose.py``, 화면 버튼 노출 시험만 남김). 실행 중인 교통
추적기의 인메모리 상태(``TrafficPipelineRunner.traffic_flow_samples()``)
가 있어야 답할 수 있어 traffic-service의 새 경로
(``/api/traffic/roi-flow/propose?camera_id=...``)로 옮겼다.

지켜야 할 것 — 옛 시험과 동일.

* **상시 처리 중이 아니면 정직하게 안내한다** — 없는 값을 지어내지 않는다
* **정지영상 해상도와 실시간 프레임 해상도가 다르면 보정한다** — 이 파일이
  반복해 겪은 함정(ROI·캘리브레이션과 같은 부류)
* **결과는 저장하지 않는다** — 저장은 사람이 기존 저장 버튼으로 확정한다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi
from tot_dashboard.service.traffic_service import app, runner

CAM_ID = "FLOWPROPOSE-CAM"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(scope="module")
def main_client(seeded_users, login):
    """ROI 저장(``/settings/cameras/{id}/roi/traffic``)은 platform-shell
    (main.app)에만 있다 — 카메라 CRUD라 옮기지 않았다(§Phase 4 상세
    설계 "남는 라우트" 참고). 제안 조회(``client``, traffic-service)와
    다른 앱이라 별도 클라이언트가 필요하다."""
    from fastapi.testclient import TestClient

    from tot_dashboard.service.main import app as main_app
    c = TestClient(main_app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


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
        cam, errs = C.create(db, {"id": CAM_ID, "name": "자동생성시험지점",
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


def _url(cam_id: str = CAM_ID) -> str:
    return f"/api/traffic/roi-flow/propose?camera_id={cam_id}"


def test_상시_처리_중이_아니면_안내한다(client):
    """이 카메라는 test_ 전용이라 실행 중인 TrafficPipelineRunner._ctx 에
    없다 — 실제 "상시 처리 아님" 상황을 그대로 재현한다(목 없이)."""
    res = client.get(_url())
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert "상시" in body["errors"][0]


def test_ROI_저장_전에는_해상도를_모른다고_안내한다(client, monkeypatch):
    samples = [(60.0, 75.0, 90.0, 75.0)] * 6
    monkeypatch.setattr(runner, "traffic_flow_samples",
                        lambda cid: (samples, (1000, 1000)))
    res = client.get(_url())
    body = res.json()
    assert body["ok"] is False
    assert "해상도" in body["errors"][0]


def test_관측이_있으면_제안을_돌려준다(client, main_client, monkeypatch):
    saved = main_client.post(f"/settings/cameras/{CAM_ID}/roi/traffic", json={
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [999, 0], [999, 999], [0, 999]]]}})
    assert saved.json()["ok"] is True

    samples = [(60.0, 75.0, 90.0, 75.0)] * 6  # 오른쪽 이동, 6건(>=min_samples)
    monkeypatch.setattr(runner, "traffic_flow_samples",
                        lambda cid: (samples, (1000, 1000)))
    res = client.get(_url())
    body = res.json()
    assert body["ok"] is True
    assert body["sample_count"] == 6
    assert len(body["arrows"]) == 1
    (x1, _y1), (x2, _y2) = body["arrows"][0]["points"]
    assert x2 > x1, "오른쪽 관측인데 제안된 화살표가 반대를 가리킨다"


def test_결과는_저장되지_않는다(client, main_client, monkeypatch):
    """제안일 뿐이다 — 이 호출만으로 DB의 flow_arrows 가 바뀌면 안 된다."""
    main_client.post(f"/settings/cameras/{CAM_ID}/roi/traffic", json={
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [999, 0], [999, 999], [0, 999]]]}})
    samples = [(60.0, 75.0, 90.0, 75.0)] * 6
    monkeypatch.setattr(runner, "traffic_flow_samples",
                        lambda cid: (samples, (1000, 1000)))
    client.get(_url())

    db = get_session()
    try:
        row = db.query(CameraRoi).filter(
            CameraRoi.camera_id == CAM_ID, CameraRoi.domain == "traffic").one()
        assert not (row.shapes or {}).get("flow_arrows")
    finally:
        db.close()


def test_실시간_해상도가_다르면_정지영상_기준으로_보정한다(client, main_client, monkeypatch):
    """정지영상(저장 ROI)은 1000x1000, 실시간 탐지 프레임은 500x500(절반)
    이라고 가정 — 이 파일이 반복해 겪은 해상도 불일치 함정과 같은 부류다."""
    main_client.post(f"/settings/cameras/{CAM_ID}/roi/traffic", json={
        "frame_width": 1000, "frame_height": 1000,
        "shapes": {"congestion_roi": [[[0, 0], [999, 0], [999, 999], [0, 999]]]}})
    # 500x500 기준으로 오른쪽 30px 이동 6건.
    samples = [(30.0, 37.5, 60.0, 37.5)] * 6
    monkeypatch.setattr(runner, "traffic_flow_samples",
                        lambda cid: (samples, (500, 500)))
    res = client.get(_url())
    body = res.json()
    assert body["ok"] is True
    (x1, y1), (x2, y2) = body["arrows"][0]["points"]
    # 보정(×2) 후 좌표는 1000x1000 공간 값이어야 한다 — 절반 공간 값(~75)
    # 그대로 나오면 화면(정지영상) 위 엉뚱한 자리에 화살표가 그려진다.
    assert x1 > 100 or x2 > 100
    assert x2 > x1


def test_카메라를_찾을_수_없으면_안내한다(client):
    res = client.get(_url("NO-SUCH-CAMERA"))
    body = res.json()
    assert body["ok"] is False
    assert "찾을 수 없습니다" in body["errors"][0]
