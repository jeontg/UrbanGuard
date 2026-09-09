"""통행 방향 화살표 자동 생성 버튼 — 화면(HTML) 노출 여부만.

⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 실제 API 동작(제안 생성·해상도
보정·저장 안 함 등)은 traffic-service로 옮겨졌다
(``test_traffic_service_flow_propose.py`` 참고, 실행 중인 교통
추적기의 인메모리 상태가 있어야 답할 수 있는 API라 그쪽 소유가 맞다).
이 파일은 그 버튼이 여전히 platform-shell이 그리는 ROI 편집기 화면
(``/settings/cameras/{id}/roi``)에 올바른 탭에서만 보이는지만 본다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi
from tot_dashboard.service.main import app

CAM_ID = "FLOWPROPOSE-CAM"


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
        cam, errs = C.create(db, {"id": CAM_ID, "name": "자동생성시험지점",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/a.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        # flood 도 함께 켠다 — "다른 도메인 탭에는 버튼이 없다" 시험이
        # domain=flood 를 요청했을 때 실제로 flood 탭을 그려야 하기
        # 때문이다(안 켠 도메인은 usable 목록에서 빠져 traffic 으로
        # 되돌아간다).
        C.set_domains(db, cam, {"traffic": {"enabled": True, "continuous": False},
                                "flood": {"enabled": True, "continuous": False}})
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def test_교통_탭에는_자동_생성_버튼이_보인다(client):
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=traffic")
    assert res.status_code == 200
    assert "btn-flow-propose" in res.text
    assert "/api/traffic/roi-flow/propose" in res.text


def test_다른_도메인_탭에는_자동_생성_버튼이_없다(client):
    """JS 핸들러 코드 자체는 어느 탭에서나 실려 있어도 무해하다
    (getElementById 가 못 찾으면 그냥 넘어간다) — 여기서 확인할 것은
    **버튼 엘리먼트 자체**가 안 그려지는지다."""
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=flood")
    assert res.status_code == 200
    assert 'id="btn-flow-propose"' not in res.text
