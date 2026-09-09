"""crowd-service의 인파 사례(케이스) 조회·미디어·알림 API.

⚠️ 2026-08-31 신설 — API 게이트웨이 Phase 1로 ``/api/cases*``·
``/media/{case_id}/*``가 platform-shell(``service/main.py``)에서
``service/crowd_service.py``(별도 서비스)로 옮겨지면서, 원래
``tests/service/test_main.py``에 있던 이 시험들을 그대로 옮겼다(로직은
바뀌지 않았다 — 실제 ``data/cases/gwangbokro-demo`` 사례 데이터를
그대로 쓴다).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.crowd_service import app


@pytest.fixture(scope="module")
def anon_client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    # /login은 platform-shell에만 있다 — 그쪽에서 로그인해 쿠키만 옮긴다
    # (conftest.py::_do_login_cross_service, 실제 배포의 쿠키 공유와 동일).
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_crowd_case_list_and_detail_and_media(client):
    cases = client.get("/api/cases").json()
    ids = [c["id"] for c in cases["cases"]]
    assert "gwangbokro-demo" in ids

    case = client.get("/api/cases/gwangbokro-demo").json()
    assert case["available_asset_count"] > 0

    media_path = case["assets"]["input_image"]["path"]
    media_res = client.get(f"/media/gwangbokro-demo/{media_path}")
    assert media_res.status_code == 200

    missing = client.get("/api/cases/does-not-exist")
    assert missing.status_code == 404


def test_crowd_notification_dry_run_send(client):
    case = client.get("/api/cases/gwangbokro-demo").json()
    message_id = case["sms_messages"][0]["id"]
    res = client.post(
        "/api/cases/gwangbokro-demo/notifications",
        json={"message_id": message_id, "channels": ["sms"]},
    )
    assert res.status_code == 200
    assert res.json()["dry_run"] is True
