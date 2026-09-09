"""flood-service(``service/flood_service.py``)의 침수 실시간 판정 API —
`/api/flood-risk`·`/api/flood-risk/{id}`·`/api/flood-history`.

⚠️ 2026-08-31 신설(API 게이트웨이 Phase 4) — `FloodPipelineRunner`가
실제로 스냅샷을 만드는지 기다린다(``test_traffic_service_risk_api.py``
와 같은 패턴). 시험 픽스처(BLOCK-TEST)는 합성(synthetic) 소스라 물
세그멘테이션 모델 자체가 안 붙는다(`_build_block_ctx`가 hls/rtsp/video
소스에만 물 모델을 올린다) — 그래서 여기서는 "물 세그멘테이션이 실제로
돌았다"가 아니라 "합성 소스에서도 정직하게 water_available=False로
응답한다"를 확인한다.

⚠️ 이 파일 하나만 ``with TestClient(app)``(lifespan 기동)을 쓴다 —
``test_traffic_service_risk_api.py``와 같은 이유.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.flood_service import app


@pytest.fixture(scope="module")
def anon_client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_침수_실시간_판정을_준다(client):
    for _ in range(20):
        risk = client.get("/api/flood-risk").json()
        if risk["blocks"]:
            break
        time.sleep(0.3)
    else:
        assert False, "FloodPipelineRunner never produced a snapshot"

    block = risk["blocks"][0]
    assert block["block_id"] == "BLOCK-TEST"
    # 합성 소스라 물 모델이 안 붙는다 — "탐지 안 함"이 아니라 정직하게
    # water_available=False로 나와야 한다(지어낸 값이면 안 된다).
    assert block["water_available"] is False

    detail = client.get("/api/flood-risk/BLOCK-TEST").json()
    assert detail["block_id"] == "BLOCK-TEST"

    history = client.get("/api/flood-history").json()
    assert "BLOCK-TEST" in history["blocks"]
