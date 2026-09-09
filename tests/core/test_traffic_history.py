"""교통 관측 이력 (core/traffic_history.py) — 2026-08-22 신설.

## 왜 이 시험이 있나

``traffic_observations`` 표는 2026-08-21 도메인 분리 때 만들어 뒀지만
**기록하는 코드가 없어 한 건도 쌓이지 않았다**(모델 docstring 이 그 사실을
명시하고 있었고, 그대로 방치돼 있었다 — 미결 과제 7A-6).

지켜야 할 것.

* ``runner._snapshot()`` 이 **실제로 내는 키**를 읽는다 — 예전에 event_sync
  가 키를 잘못 읽어 detail 이 늘 비어 있던 전례가 있다
* **못 본 것과 원활한 것을 구분한다**(``failed``)
* 「봤는데 평온했다」도 남는다 — 안 남기면 기준선을 만들 수 없다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import traffic_history as TH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import TrafficObservation

pytestmark = pytest.mark.usefixtures("db_schema")

CAM = "TEST-TH-A"


def _purge():
    db = get_session()
    try:
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


@pytest.fixture()
def db():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _snapshot(**over) -> dict:
    """``runner._snapshot()`` 이 실제로 내는 키 이름 그대로."""
    base = {
        "block_id": CAM, "name": "시험지점",
        "rain_mm_h": 12.5, "speed_drop": 0.42,
        "queue_len": 7, "stalled": 3,
        "risk_code": "TWR_RAIN_CONGESTION", "score": 0.63, "severity": 2,
        "drivers": ["강수 강함(12.5mm/h)", "속도 42% 감소"],
        "source_kind": "hls",
    }
    base.update(over)
    return base


def test_스냅샷_키를_실제_이름대로_읽는다(db):
    """★ 핵심 — 키를 하나라도 잘못 읽으면 그 칸이 늘 0/빈값으로 쌓인다."""
    TH.record(db, camera_id=CAM, camera_name="시험지점",
              snapshot=_snapshot(), source="hls")
    db.commit()

    row = db.scalar(select(TrafficObservation).where(
        TrafficObservation.camera_id == CAM))
    assert row.rain_mm_h == pytest.approx(12.5)
    assert row.speed_drop == pytest.approx(0.42)
    assert row.queue_len == 7
    assert row.stalled_count == 3          # 스냅샷 키는 "stalled"
    assert row.risk_code == "TWR_RAIN_CONGESTION"
    assert row.risk_score == pytest.approx(0.63)   # 스냅샷 키는 "score"
    assert row.severity == 2
    assert "강수 강함" in row.drivers
    assert row.source == "hls"
    assert row.failed is False


def test_평온한_관측도_남는다(db):
    """「봤는데 아무 일 없었다」가 빠지면 기준선을 만들 수 없다."""
    TH.record(db, camera_id=CAM, snapshot=_snapshot(
        speed_drop=0.0, queue_len=0, stalled=0, severity=0, score=0.0))
    db.commit()
    assert len(TH.recent(db, CAM)) == 1


def test_못_본_것과_원활한_것을_구분한다(db):
    TH.record(db, camera_id=CAM, snapshot=None, failed=True)
    db.commit()
    row = TH.recent(db, CAM)[0]
    assert row.failed is True
    # series() 는 실패 관측을 뺀다 — 0 으로 채우면 예측이 「원활했다」로 학습한다.
    assert TH.series(db, CAM) == []


def test_카메라_id가_없으면_아무것도_안_한다(db):
    assert TH.record(db, camera_id="", snapshot=_snapshot()) is None
    assert TH.record(db, camera_id="   ", snapshot=_snapshot()) is None


def test_기준선은_관측이_없으면_빈_dict(db):
    """0 을 돌려주면 「평소에 전혀 안 막힌다」로 읽혀 지금이 전부 급변으로 보인다."""
    assert TH.baseline(db, CAM) == {}


def test_기준선을_계산한다(db):
    for drop in (0.1, 0.3, 0.5):
        TH.record(db, camera_id=CAM, snapshot=_snapshot(speed_drop=drop))
    db.commit()
    bl = TH.baseline(db, CAM)
    assert bl["samples"] == 3
    assert bl["speed_drop_median"] == pytest.approx(0.3)
    assert bl["speed_drop_max"] == pytest.approx(0.5)


def test_보존기간이_지난_것만_지운다(db):
    TH.record(db, camera_id=CAM, snapshot=_snapshot())
    db.commit()
    assert TH.purge(db, keep_days=400) == 0     # 방금 넣은 것은 안 지운다
    assert TH.purge(db, keep_days=0) == 0       # 0 이하는 아무것도 안 한다
