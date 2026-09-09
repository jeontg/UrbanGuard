"""밀집도 등급이 이벤트로 올라가는가 (2026-08-19 전수조사).

## 왜 이 시험이 있나

전수조사에서 인파 이벤트 11건이 **전부 배회·침입 경로**였다. 밀집도는 이력만
남기고 이벤트를 만들지 않아, **`severity 3`(군중급증위험) 5회가 이벤트가
되지 않았다.** 경남 SFR-008 은 「인파 밀집 감지」를 요구한다.

## ⚠️ 그런데 문턱대로 다 올리면 안 된다

같은 조사에서 관측 **670회 중 627회(93.6%)가 `severity 2`** 였다. 판정 문턱이
교차로 특성과 맞지 않아 「이동흐름혼란」이 상시 발동하기 때문이다.
전부 이벤트로 올리면 **이벤트 큐가 평상시에도 가득 차 무의미해진다.**

★ 그래서 **`severity 3` 이상만** 올린다. 이 경계가 이 시험들의 알맹이다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Event, EventAction
from tot_dashboard.service import event_sync

CAM = "TEST-CDE-A"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(EventAction).where(
            EventAction.event_id.in_(
                select(Event.id).where(Event.block_id == CAM))))
        db.execute(sa_delete(Event).where(Event.block_id == CAM))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def _snap(severity: int) -> dict:
    return {"severity": severity, "person_count": 20, "density_index": 0.8,
            "risk_name": "군중급증위험", "risk_code": "CROWD_SURGE_RISK",
            "risk_score": 0.7, "drivers": ["급증"]}


def _events():
    db = get_session()
    try:
        return list(db.scalars(
            select(Event).where(Event.block_id == CAM)))
    finally:
        db.close()


@pytest.mark.parametrize("sev", [0, 1, 2])
def test_쏠린_등급은_이벤트로_올리지_않는다(sev):
    """★ 관측 93.6%가 등급 2다. 올리면 이벤트 큐가 평상시에도 가득 찬다."""
    event_sync.record_crowd_observation(CAM, "시험지점", _snap(sev))
    assert _events() == []


def test_군중급증위험은_경계로_올린다():
    event_sync.record_crowd_observation(CAM, "시험지점", _snap(3))
    evs = _events()
    assert len(evs) == 1
    assert evs[0].level == "경계"


def test_패닉분산은_심각으로_올린다():
    event_sync.record_crowd_observation(CAM, "시험지점", _snap(4))
    evs = _events()
    assert len(evs) == 1
    assert evs[0].level == "심각"


def test_판정_근거를_함께_남긴다():
    """⚠️ 「경계」만 뜨고 왜인지 없으면 관제요원이 확인할 것을 못 찾는다."""
    event_sync.record_crowd_observation(CAM, "시험지점", _snap(3))
    d = _events()[0].detail or {}
    assert d.get("근거")
    assert d.get("판정")


def test_빈_스냅샷은_아무것도_안_한다():
    """프레임을 못 받은 것을 위험으로 읽으면 안 된다."""
    event_sync.record_crowd_observation(CAM, "시험지점", None)
    assert _events() == []
