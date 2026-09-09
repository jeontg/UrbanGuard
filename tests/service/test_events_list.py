"""S-02 이벤트 목록 — 자동 보류 필터·설정 (2026-09-02 신설).

지켜야 할 것.

* **자동 보류는 "자동 보류" 필터로만 보인다** — "진행 중"(active)
  기본 필터에는 안 뜬다(멀리서 방치를 놓치지 않으려면 여기서 찾을 수
  있어야 한다).
* **정책 설정은 이벤트 관리 화면(S-02)에서 직접 바꿀 수 있다** —
  별도 설정 화면이 아니라 이 화면 자체에.
* **잘못된 시간값(0 이하·숫자 아님)은 거부**한다.
* **사람의 이벤트 종결이 감사 로그(S-61)에 남는다** — 이번에 함께
  메운 기존 공백.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import audit
from tot_dashboard.core import events as E
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog, Event, EventAction
from tot_dashboard.service.main import app


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
        s.execute(sa_delete(EventAction))
        s.execute(sa_delete(Event))
        s.execute(sa_delete(AuditLog))
        s.commit()
    finally:
        s.close()
    S.invalidate()


def _stale_event(db, *, block="BLOCK-TEST") -> Event:
    from datetime import datetime, timedelta, timezone
    ev = Event(domain="flood", block_id=block, place_name="시험지점",
              event_type="침수", level="주의", peak_level="주의",
              status=E.OPEN,
              last_detected_at=datetime.now(timezone.utc) - timedelta(hours=25))
    db.add(ev)
    db.flush()
    return ev


def test_자동_보류_필터로만_보인다(client):
    db = get_session()
    try:
        _stale_event(db)
        db.commit()
        E.auto_hold_stale(db, threshold_hours=24)
        db.commit()
    finally:
        db.close()

    active = client.get("/events?status=active")
    assert "시험지점" not in active.text

    held = client.get("/events?status=auto_held")
    assert held.status_code == 200
    assert "시험지점" in held.text
    assert "자동 보류" in held.text


def test_정책_설정을_저장할_수_있다(client):
    r = client.post("/events/auto-hold-settings",
                    data={"enabled": "1", "hours": "48"})
    assert r.status_code == 200
    assert "저장했습니다" in r.text

    db = get_session()
    try:
        assert S.event_auto_hold_enabled(db) is True
        assert S.event_auto_hold_hours(db) == 48.0
    finally:
        db.close()


def test_0_이하나_숫자가_아닌_시간은_거부한다(client):
    r = client.post("/events/auto-hold-settings",
                    data={"enabled": "1", "hours": "0"})
    assert r.status_code == 400
    r2 = client.post("/events/auto-hold-settings",
                     data={"enabled": "1", "hours": "abc"})
    assert r2.status_code == 400


def test_끄면_방치돼도_자동_보류로_안_옮겨진다(client):
    client.post("/events/auto-hold-settings", data={"enabled": "0", "hours": "24"})
    db = get_session()
    try:
        assert S.event_auto_hold_enabled(db) is False
    finally:
        db.close()
    client.post("/events/auto-hold-settings", data={"enabled": "1", "hours": "24"})


def test_관제요원은_정책을_저장할_수_없다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        r = anon_client.post("/events/auto-hold-settings",
                             data={"enabled": "1", "hours": "24"})
        assert r.status_code == 403
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


def test_사람이_종결하면_감사로그에_남는다(client):
    db = get_session()
    try:
        ev = Event(domain="flood", block_id="BLOCK-CLOSE", place_name="종결시험",
                  event_type="침수", level="주의", peak_level="주의", status=E.OPEN)
        db.add(ev)
        db.commit()
        ev_id = ev.id
    finally:
        db.close()

    r = client.post(f"/events/{ev_id}/action", data={"act": "close", "memo": ""})
    assert r.status_code == 200

    db = get_session()
    try:
        acts = [a for (a,) in db.query(AuditLog.action)
               .filter(AuditLog.action == audit.EVENT_CLOSE).all()]
        assert len(acts) >= 1
    finally:
        db.close()
