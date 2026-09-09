"""인파 상시 카메라별 모니터링 API (/api/crowd/continuous, 2026-08-27 신설).

**신고받은 요청**: "인파관리도 교통위험처럼 CCTV별로 모니터링하여 분석하는
기능이 있어야 하지 않나요?" — 조사해 보니 「실시간 분석 (상시)」는 39개소
중 아무 기준 없이 첫 번째 카메라 하나만 보여주고 있었다
(`_init_crowd_analyzer`의 `BLOCKS[0]`). 이 경로는 S-80에서 「인파·상시」로
지정한 카메라 **전부**를 각자 보여준다(`continuous.CrowdContinuousWatcher`가
카메라마다 별도 스레드로 이미 관측하고 있었다 — 결과를 보여주는 화면이
없었을 뿐).

지켜야 할 것.

* **권한은 실시간 조회와 같다** — `/api/crowd/live`와 같은 규칙(guard.py)
* **관측이 없으면 "미관측"으로 정직하게 답한다** — 0명으로 채우지 않는다
* **워처가 아직 못 붙은(재기동 대기) 카메라는 `pending`으로 구분된다** —
  설정(DB)은 상시인데 실제로 관측되지 않는 상태를 감춰선 안 된다
* **사용자 지시대로, 신설 직후 실제 6개 카메라는 전부 정지 상태다** —
  이 시험은 그 사실 자체를 회귀 방지로 고정한다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core import crowd_history as CH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraRoi, CrowdObservation
from tot_dashboard.service.crowd_service import app

PFX = "TEST-CROWD-CONT-"
CAM_A = f"{PFX}A"
CAM_B = f"{PFX}B"

# 사용자 요청으로 초기값을 전부 정지시킨 실제 운영 카메라 6개.
REAL_CONTINUOUS_CAMS = ["SEOUL-113", "SEOUL-1042", "SEOUL-19",
                        "BLOCK-BUSANSTN", "SEOUL-207", "BLOCK-JAESONG"]


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    # ⚠️ 2026-08-31 — /api/crowd/continuous는 API 게이트웨이 Phase 1로
    # crowd_service.app(별도 서비스)으로 옮겨졌다. /login은 platform-shell
    # 에만 있으므로, 그쪽에서 로그인해 쿠키만 이 클라이언트에 옮긴다
    # (실제 배포에서 두 서비스가 세션 쿠키를 공유하는 것과 동일한 방식).
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(CrowdObservation)
                   .where(CrowdObservation.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(CameraRoi).where(CameraRoi.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _cams(db_schema):
    db = get_session()
    try:
        camA, errs = C.create(db, {"id": CAM_A, "name": "인파상시A",
                                   "source_type": "hls",
                                   "source_url": "https://example.test/a.m3u8",
                                   "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        camB, errs = C.create(db, {"id": CAM_B, "name": "인파상시B",
                                   "source_type": "hls",
                                   "source_url": "https://example.test/b.m3u8",
                                   "lat": "35.2", "lng": "129.1"})
        assert not errs, errs
        C.set_domains(db, camA, {"crowd": {"enabled": True, "continuous": True}})
        C.set_domains(db, camB, {"crowd": {"enabled": True, "continuous": True}})
        db.commit()
        CH.record(db, camera_id=CAM_A, camera_name="인파상시A",
                 snapshot={"person_count": 12, "density_index": 0.5,
                          "mean_speed": 8.0, "severity": 1,
                          "risk_code": "CROWD_DENSITY_HIGH", "risk_score": 0.4,
                          "drivers": ["밀집도UP"], "source": "detector"})
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def test_로그인하지_않으면_막힌다(anon_client, db_schema):
    anon_client.cookies.clear()
    r = anon_client.get("/api/crowd/continuous")
    assert r.status_code in (401, 403)


def test_권한이_실시간_조회와_같다():
    """이 화면으로 우회해 더 볼 수 있으면 안 된다."""
    from tot_dashboard.core import guard

    a = guard._match("GET", "/api/crowd/continuous")
    b = guard._match("GET", "/api/crowd/live")
    assert a is not None, "등록하지 않으면 fail-closed로 막힌다"
    assert a == b, f"권한이 다르다: 상시 {a} / 실시간 {b}"


def test_상시_지정_카메라_전부가_각자_나온다(client):
    r = client.get("/api/crowd/continuous")
    assert r.status_code == 200
    body = r.json()
    ids = {p["camera_id"] for p in body["points"]}
    assert {CAM_A, CAM_B} <= ids


def test_관측이_있는_카메라는_최근_값을_보여준다(client):
    r = client.get("/api/crowd/continuous")
    body = r.json()
    a = next(p for p in body["points"] if p["camera_id"] == CAM_A)
    assert a["observed"] is True
    assert a["person_count"] == 12
    assert a["severity"] == 1
    assert a["source"] == "detector"


def test_관측이_없는_카메라는_미관측으로_정직하게_답한다(client):
    r = client.get("/api/crowd/continuous")
    body = r.json()
    b = next(p for p in body["points"] if p["camera_id"] == CAM_B)
    assert b["observed"] is False
    assert b["severity"] is None


def test_워처가_아직_안_붙은_카메라는_pending으로_표시된다(client):
    """방금 「탐지 지정」에서 켠 카메라 — 다음 재기동 전까지는 워처
    타깃 목록에 없다. 화면이 이걸 「관측 중」으로 오해하면 안 된다."""
    r = client.get("/api/crowd/continuous")
    body = r.json()
    for p in body["points"]:
        if p["camera_id"] in (CAM_A, CAM_B):
            # 시험 환경에서는 워처를 띄우지 않으므로 둘 다 pending이어야 한다.
            assert p["pending"] is True


def test_실제_운영_카메라_6개는_초기값이_전부_정지다():
    """★ 회귀 방지 — 사용자 지시: "초기 설정에서는 6개 모두 모니터링
    정지로 설정해 주고 실제 모니터링(탐지)도 중지". 관리자가 CCTV 관리
    화면에서 개별적으로 다시 켜기 전까지는 계속 정지 상태여야 한다."""
    db = get_session()
    try:
        for cid in REAL_CONTINUOUS_CAMS:
            cam = C.get(db, cid)
            if cam is None:
                continue  # 이 환경에 해당 카메라가 없으면 건너뛴다
            row = cam.domain_row("crowd")
            assert row is not None
            assert row.enabled is False, f"{cid}: 인파 상시 지정이 켜져 있습니다"
            assert row.continuous is False, f"{cid}: 인파 상시 continuous가 켜져 있습니다"
    finally:
        db.close()
