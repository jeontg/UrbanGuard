"""인파 관측 이력 API와 화면 (/api/crowd/history · app.js).

지켜야 할 것.

* **관측이 없으면 아무 말도 하지 않는다** — 「평소 0명」이라고 쓰면 지금
  인원이 전부 급증으로 읽힌다
* **권한은 실시간 조회와 같다** — 이력으로 우회해 더 볼 수 없어야 한다
* **화면이 실제로 그린다** — 서버가 보내도 화면이 안 쓰면 아무 일도 안 일어난다
* **기준선 조회가 실시간 표시를 막지 않는다** — 따로 불러 온다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import crowd_history as CH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import CrowdObservation

CAM = "TEST-HIST-API"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(CrowdObservation)
                   .where(CrowdObservation.camera_id.like("TEST-HIST%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def clean(db_schema):
    _purge()
    yield
    _purge()


@pytest.fixture(scope="module")
def anon_client():
    from fastapi.testclient import TestClient

    # ⚠️ 2026-08-31 — /api/crowd/history는 API 게이트웨이 Phase 1로
    # crowd_service.app(별도 서비스)으로 옮겨졌다.
    from tot_dashboard.service.crowd_service import app
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    # /login은 platform-shell에만 있으므로, 그쪽에서 로그인해 쿠키만
    # 이 클라이언트에 옮긴다(conftest.py::_do_login_cross_service 참고).
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


# --- 권한 --------------------------------------------------------------------
def test_로그인하지_않으면_막힌다(anon_client, db_schema):
    anon_client.cookies.clear()
    r = anon_client.get(f"/api/crowd/history/{CAM}")
    assert r.status_code in (401, 403)


def test_권한이_실시간_조회와_같다():
    """이력으로 우회해 더 볼 수 있으면 안 된다."""
    from tot_dashboard.core import guard

    a = guard._match("GET", f"/api/crowd/history/{CAM}")
    b = guard._match("GET", "/api/crowd/live")
    assert a is not None, "등록하지 않으면 fail-closed 로 막힌다"
    assert a == b, f"권한이 다르다: 이력 {a} / 실시간 {b}"


# --- 응답 --------------------------------------------------------------------
def test_관측이_없으면_기준선이_비어_있다(client, clean):
    """0 을 주면 지금 인원이 전부 급증으로 보인다."""
    r = client.get(f"/api/crowd/history/{CAM}")
    assert r.status_code == 200
    d = r.json()
    assert d["baseline"] == {}
    assert d["observations"] == []


def test_관측이_있으면_기준선을_준다(client, clean):
    db = get_session()
    try:
        for n in (4, 6, 8):
            CH.record(db, camera_id=CAM, camera_name="시험지점",
                      snapshot={"person_count": n, "surge": 1.1,
                                "risk_code": "NORMAL"})
        db.commit()
    finally:
        db.close()

    d = client.get(f"/api/crowd/history/{CAM}").json()
    assert d["baseline"]["samples"] == 3
    assert d["baseline"]["person_median"] == 6
    assert len(d["observations"]) == 3
    assert d["observations"][0]["person_count"] == 8   # 최근이 먼저


def test_응답에_흐름과_근거가_들어간다(client, clean):
    db = get_session()
    try:
        CH.record(db, camera_id=CAM,
                  snapshot={"person_count": 20, "surge": 1.9,
                            "divergence": 8.1, "risk_code": "CROWD_SURGE_RISK",
                            "drivers": ["속도급증"]})
        db.commit()
    finally:
        db.close()

    o = client.get(f"/api/crowd/history/{CAM}").json()["observations"][0]
    assert o["surge"] == pytest.approx(1.9)
    assert o["divergence"] == pytest.approx(8.1)
    assert "속도급증" in o["drivers"]
    assert o["risk_code"] == "CROWD_SURGE_RISK"


def test_다른_지점은_섞이지_않는다(client, clean):
    db = get_session()
    try:
        CH.record(db, camera_id=CAM, snapshot={"person_count": 5})
        CH.record(db, camera_id="TEST-HIST-OTHER", snapshot={"person_count": 99})
        db.commit()
    finally:
        db.close()

    obs = client.get(f"/api/crowd/history/{CAM}").json()["observations"]
    assert [o["person_count"] for o in obs] == [5]


# --- 화면 --------------------------------------------------------------------
def test_화면이_기준선을_그린다():
    """서버가 보내도 화면이 안 쓰면 아무 일도 안 일어난다."""
    import pathlib

    js = (pathlib.Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
          / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function loadCrowdBaseline(" in js
    assert "loadCrowdBaseline(" in js.replace("function loadCrowdBaseline(", "", 1), (
        "만들어 놓고 부르지 않으면 안 보인다")
    assert "/api/crowd/history/" in js
    # 관측이 없을 때 0 을 쓰지 않고 「만드는 중」이라고 말한다.
    assert "기준선을 만드는 중" in js


def test_기준선_칸에_스타일이_있다():
    import pathlib

    css = (pathlib.Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
           / "service" / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".crowd-baseline" in css
