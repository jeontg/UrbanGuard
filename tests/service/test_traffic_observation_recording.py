"""``_sync_traffic`` 이 관측 이력을 남기는가 (2026-08-22, 미결 과제 7A-6).

이벤트는 「주의」 이상만 만들어진다(``events.EVENT_THRESHOLD``). 그런데
**관측 이력은 이벤트 생성 여부와 무관하게 매번 남아야 한다** — 「봤는데
평온했다」가 빠지면 평상시 기준선을 만들 수 없다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import traffic_history as TH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Event, EventAction, TrafficObservation
from tot_dashboard.service import event_sync

pytestmark = pytest.mark.usefixtures("db_schema")

CAM = "TEST-TOR-A"


class _Store:
    def __init__(self, snaps):
        self._snaps = snaps

    def all(self):
        return self._snaps


def _purge():
    db = get_session()
    try:
        from sqlalchemy import select
        db.execute(sa_delete(EventAction).where(
            EventAction.event_id.in_(select(Event.id).where(Event.block_id == CAM))))
        db.execute(sa_delete(Event).where(Event.block_id == CAM))
        db.execute(sa_delete(TrafficObservation).where(
            TrafficObservation.camera_id == CAM))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean():
    _purge()
    yield
    _purge()


def _snap(level: str) -> dict:
    return {"block_id": CAM, "name": "시험지점", "level": level,
            "rain_mm_h": 8.0, "speed_drop": 0.3, "queue_len": 4,
            "stalled": 1, "risk_code": "TWR_RAIN_ONSET", "score": 0.4,
            "severity": 1, "drivers": ["강수"], "source_kind": "hls",
            "n_vehicles": 12, "mean_speed_kmh": 21.0, "intensity": "moderate"}


def test_이벤트가_안_생겨도_관측은_남는다():
    """★ 핵심 — 「관심」은 이벤트가 안 되지만 관측 이력은 남아야 한다."""
    db = get_session()
    try:
        event_sync._sync_traffic(db, _Store([_snap("관심")]))
        db.commit()
        assert len(TH.recent(db, CAM)) == 1, "평온한 관측이 기록되지 않았다"
        from sqlalchemy import select
        assert db.scalars(select(Event).where(Event.block_id == CAM)).all() == []
    finally:
        db.close()


def test_이벤트가_생겨도_관측은_따로_남는다():
    db = get_session()
    try:
        event_sync._sync_traffic(db, _Store([_snap("주의")]))
        db.commit()
        assert len(TH.recent(db, CAM)) == 1
        from sqlalchemy import select
        assert len(db.scalars(select(Event).where(Event.block_id == CAM)).all()) == 1
    finally:
        db.close()


def test_block_id가_없으면_건너뛴다():
    db = get_session()
    try:
        event_sync._sync_traffic(db, _Store([{"name": "이름만"}]))
        db.commit()
        assert TH.recent(db, CAM) == []
    finally:
        db.close()
