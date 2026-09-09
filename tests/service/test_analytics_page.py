"""S-60 통계·성과 리포트 화면 (Phase 3, 2026-08-26 위험유형별 오탐률 추가).

지켜야 할 것.

* **판정 없는 유형은 표에 안 뜬다** — 0건을 「오탐 없음」으로 읽으면 안 된다
* **표본이 적으면 비율 대신 표본 수** — 5건 중 0건 오탐을 "0%"라 하면
  실제보다 좋아 보인다
* **재현율(미탐률)은 화면 어디에도 산출하지 않는다** — 분모(실제 발생 전체
  사건)를 모른다
* CSV 내보내기에도 같은 표가 실린다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import events as E
from tot_dashboard.core import feedback as FB
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (VERDICT_FALSE, VERDICT_TRUE,
                                       DetectionFeedback, Event)
from tot_dashboard.service.main import app

PFX = "TEST-ANLQ-"


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
        db.execute(sa_delete(DetectionFeedback).where(
            DetectionFeedback.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def test_판정_없으면_유형별_오탐률_표가_비어있다는_안내가_뜬다(client):
    res = client.get("/analytics")
    assert res.status_code == 200
    assert "위험유형별 오탐률" in res.text
    assert "재현율(미탐률)" in res.text  # 고정 문구가 항상 떠야 한다


def test_판정이_있으면_유형별_오탐률이_보인다(client):
    db = get_session()
    try:
        V.seed_builtin(db)
        ev = E.record_detection(
            db, domain="traffic", block_id=f"{PFX}A", place_name="시험지점",
            level="주의", hazard_type_code="traffic_pedestrian",
            split_by_hazard_type=True)
        db.commit()
        FB.record(db, verdict=VERDICT_FALSE, event=ev, reason="그림자")
        db.commit()
    finally:
        db.close()

    res = client.get("/analytics")
    assert res.status_code == 200
    assert "보행자 도로 진입" in res.text


def test_표본이_적으면_비율_대신_표본_수가_보인다(client):
    db = get_session()
    try:
        V.seed_builtin(db)
        ev = E.record_detection(
            db, domain="traffic", block_id=f"{PFX}B", place_name="시험지점",
            level="주의", hazard_type_code="traffic_pedestrian",
            split_by_hazard_type=True)
        db.commit()
        FB.record(db, verdict=VERDICT_TRUE, event=ev)
        db.commit()
    finally:
        db.close()

    res = client.get("/analytics")
    assert res.status_code == 200
    assert "표본 1건" in res.text


def test_CSV_내보내기에도_유형별_오탐률이_실린다(client):
    db = get_session()
    try:
        V.seed_builtin(db)
        ev = E.record_detection(
            db, domain="traffic", block_id=f"{PFX}C", place_name="시험지점",
            level="주의", hazard_type_code="traffic_pedestrian",
            split_by_hazard_type=True)
        db.commit()
        FB.record(db, verdict=VERDICT_FALSE, event=ev, reason="그림자")
        db.commit()
    finally:
        db.close()

    res = client.get("/analytics/export")
    assert res.status_code == 200
    assert "유형별 오탐률" in res.text
    assert "보행자 도로 진입" in res.text
