"""traffic-service(``service/traffic_service.py``)의 교통위험 실시간
판정 API — `/api/risk`·`/api/risk/{id}`·`/api/history`.

⚠️ 2026-08-31 신설 — API 게이트웨이 Phase 4로 이 API들이
platform-shell(``service/main.py``)에서 이 별도 서비스로 옮겨지면서,
원래 ``tests/service/test_main.py``의
``test_flood_blocks_and_risk_endpoints``에 있던 검증(러너가 실제로
스냅샷을 만드는지 기다리는 부분)을 그대로 옮겼다.

⚠️ 이 파일 하나만 ``with TestClient(app)``(lifespan 기동)을 쓴다 —
``TrafficPipelineRunner``는 스레드라 한 프로세스에서 한 번만 시작할 수
있다(``test_road_service_api.py``와 같은 이유). 다른 traffic_service
시험 파일은 평범한 ``TestClient(app)``만 쓴다.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.traffic_service import app


@pytest.fixture(scope="module")
def anon_client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_교통위험_실시간_판정을_준다(client):
    # 백그라운드 TrafficPipelineRunner가 스냅샷을 만들 때까지 기다린다.
    for _ in range(20):
        risk = client.get("/api/risk").json()
        if risk["blocks"]:
            break
        time.sleep(0.3)
    else:
        assert False, "TrafficPipelineRunner never produced a risk snapshot"

    block = risk["blocks"][0]
    assert block["block_id"] == "BLOCK-TEST"
    assert block["level"] in ("관심", "주의", "경계", "심각")

    detail = client.get("/api/risk/BLOCK-TEST").json()
    assert "history" in detail

    history = client.get("/api/history").json()
    assert "BLOCK-TEST" in history["blocks"]
