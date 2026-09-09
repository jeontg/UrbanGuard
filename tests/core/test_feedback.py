"""탐지 피드백 (core/feedback.py) — S-07.

지켜야 할 것.

* **오탐·미탐은 사유가 필수** — 「무엇을 잘못 봤는가」가 모델 개선의 알맹이라,
  사유 없는 판정은 숫자만 남고 쓸 데가 없다
* **판정을 고쳐 쓰지 않고 쌓는다** — 「오탐이라 했다가 정탐으로 바꿨다」는
  사실 자체가 모델 평가에 필요하다
* ⚠️ **오탐률에서 판단 보류와 미탐을 뺀다** — 섞으면 숫자가 무슨 뜻인지
  아무도 모른다
* ⚠️ **판정이 없으면 오탐률을 만들지 않는다** — 0% 는 「오탐이 없다」로
  읽히는데 실제로는 「아직 아무도 안 봤다」다
* **진행 중인 사건은 판정 대기에 넣지 않는다** — 지금 대응할 일이지
  판정할 일이 아니다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import feedback as FB
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (VERDICT_FALSE, VERDICT_MISSED,
                                       VERDICT_TRUE, VERDICT_UNCLEAR,
                                       DetectionFeedback, Event)

PFX = "TEST-FB-"


def _purge(db):
    db.execute(sa_delete(DetectionFeedback).where(
        DetectionFeedback.camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
    db.commit()


@pytest.fixture
def db(db_schema):
    s = get_session()
    _purge(s)
    yield s
    _purge(s)
    s.close()


class _User:
    id = None
    login_id = "tester"


def _ev(db, *, status="closed", level="경계", block=None):
    ev = Event(domain="flood", block_id=block or f"{PFX}A",
               place_name="시험지점", event_type="침수", level=level,
               status=status)
    db.add(ev)
    db.commit()
    return ev


# --- 판정 -------------------------------------------------------------------


def test_true_positive_needs_no_reason(db):
    ev = _ev(db)
    row, errs = FB.record(db, verdict=VERDICT_TRUE, user=_User(), event=ev)
    assert errs == [] and row is not None
    assert row.camera_id == f"{PFX}A"
    assert row.level == "경계"          # 판정 당시 등급이 굳혀진다


def test_false_positive_requires_reason(db):
    ev = _ev(db)
    row, errs = FB.record(db, verdict=VERDICT_FALSE, user=_User(), event=ev)
    assert row is None
    assert errs and "사유" in errs[0]


def test_false_positive_with_reason_is_saved(db):
    ev = _ev(db)
    row, errs = FB.record(db, verdict=VERDICT_FALSE, user=_User(), event=ev,
                          reason="수면 반사를 물로 봤음")
    db.commit()
    assert errs == []
    assert row.reason == "수면 반사를 물로 봤음"


def test_unclear_needs_no_reason(db):
    """「모르겠다」가 정직한 답인 경우가 실제로 있다."""
    ev = _ev(db)
    _row, errs = FB.record(db, verdict=VERDICT_UNCLEAR, user=_User(), event=ev)
    assert errs == []


def test_bad_verdict_is_rejected(db):
    ev = _ev(db)
    row, errs = FB.record(db, verdict="whatever", user=_User(), event=ev)
    assert row is None and errs


def test_event_false_positive_flag_follows_verdict(db):
    """화면이 events.false_positive 를 쓰고 있어 맞춰 둬야 한다."""
    ev = _ev(db)
    FB.record(db, verdict=VERDICT_FALSE, user=_User(), event=ev, reason="오탐")
    db.commit()
    assert ev.false_positive is True

    FB.record(db, verdict=VERDICT_TRUE, user=_User(), event=ev)
    db.commit()
    assert ev.false_positive is False


def test_verdicts_stack_not_overwrite(db):
    """★ 판정을 고쳐 쓰지 않고 쌓는다."""
    ev = _ev(db)
    FB.record(db, verdict=VERDICT_FALSE, user=_User(), event=ev, reason="오탐")
    db.commit()
    FB.record(db, verdict=VERDICT_TRUE, user=_User(), event=ev)
    db.commit()

    hist = FB.history_for_event(db, ev.id)
    assert len(hist) == 2
    assert FB.latest_for_event(db, ev.id).verdict == VERDICT_TRUE


# --- 미탐 -------------------------------------------------------------------


def test_missed_needs_camera_and_time(db):
    row, errs = FB.record(db, verdict=VERDICT_MISSED, user=_User(),
                          reason="놓쳤음")
    assert row is None
    assert any("지점" in e for e in errs)
    assert any("시각" in e for e in errs)


def test_missed_has_no_event(db):
    from datetime import datetime, timezone
    row, errs = FB.record(db, verdict=VERDICT_MISSED, user=_User(),
                          camera_id=f"{PFX}A", domain="flood",
                          reason="지하차도 침수 20분간 미탐",
                          occurred_at=datetime(2026, 8, 19, 3, 0,
                                               tzinfo=timezone.utc))
    db.commit()
    assert errs == []
    assert row.event_id is None          # 붙일 이벤트가 없다
    assert row.verdict == VERDICT_MISSED


# --- 집계 -------------------------------------------------------------------


def test_false_rate_is_none_without_judgements(db):
    """⚠️ 0% 는 「오탐이 없다」로 읽힌다 — 실제로는 「아직 안 봤다」다."""
    got = FB.counts(db, domain="nonexistent-domain")
    assert got["false_rate"] is None


def test_false_rate_excludes_unclear_and_missed(db):
    """★ 판단 보류와 미탐은 분모에서 빠진다."""
    from datetime import datetime, timezone
    for _ in range(3):
        FB.record(db, verdict=VERDICT_TRUE, user=_User(),
                  event=_ev(db, block=f"{PFX}A"))
    FB.record(db, verdict=VERDICT_FALSE, user=_User(),
              event=_ev(db, block=f"{PFX}A"), reason="오탐")
    FB.record(db, verdict=VERDICT_UNCLEAR, user=_User(),
              event=_ev(db, block=f"{PFX}A"))
    FB.record(db, verdict=VERDICT_MISSED, user=_User(), camera_id=f"{PFX}A",
              domain="flood", reason="미탐",
              occurred_at=datetime(2026, 8, 19, tzinfo=timezone.utc))
    db.commit()

    got = FB.counts(db, domain="flood")
    assert got["judged"] == 4                       # 정탐 3 + 오탐 1
    assert got["false_rate"] == 25.0                # 1/4 — 보류·미탐 제외
    assert got[VERDICT_UNCLEAR] == 1
    assert got[VERDICT_MISSED] == 1


# --- 판정 대기 --------------------------------------------------------------


def test_pending_excludes_open_events(db):
    """진행 중인 사건을 두고 오탐이냐 묻는 것은 순서가 틀렸다."""
    _ev(db, status="open")
    closed = _ev(db, status="closed")
    ids = {e.id for e in FB.pending_events(db)}
    assert closed.id in ids
    assert all(e.status == "closed" for e in FB.pending_events(db))


def test_pending_excludes_judged(db):
    ev = _ev(db)
    assert ev.id in {e.id for e in FB.pending_events(db)}
    FB.record(db, verdict=VERDICT_TRUE, user=_User(), event=ev)
    db.commit()
    assert ev.id not in {e.id for e in FB.pending_events(db)}


def test_pending_respects_domain_scope(db):
    ev = _ev(db)
    assert ev.id not in {e.id for e in
                         FB.pending_events(db, allowed_domains={"road"})}
    assert ev.id in {e.id for e in
                     FB.pending_events(db, allowed_domains={"flood"})}


# --- 유형별 집계 (Phase 3, 2026-08-26) ---------------------------------------


def _ev_typed(db, *, hazard_type_code: str, domain: str = "traffic",
             block: str | None = None):
    ev = Event(domain=domain, block_id=block or f"{PFX}A",
              place_name="시험지점", event_type="교통위험", level="경계",
              status="closed", hazard_type_code=hazard_type_code)
    db.add(ev)
    db.commit()
    return ev


def test_유형별로_따로_집계된다(db):
    """★ 강우정체와 보행자를 한 줄로 합치면 어느 판정 로직을 손봐야
    하는지 알 수 없다(docs/202608260842/)."""
    FB.record(db, verdict=VERDICT_TRUE, user=_User(),
              event=_ev_typed(db, hazard_type_code="traffic_rain_congestion"))
    FB.record(db, verdict=VERDICT_FALSE, user=_User(),
              event=_ev_typed(db, hazard_type_code="traffic_pedestrian"),
              reason="그림자를 보행자로 오인")
    db.commit()

    rows = {r["hazard_type_code"]: r for r in FB.counts_by_hazard(db)}
    assert rows["traffic_rain_congestion"]["false_rate"] == 0.0  # 정탐 1건, 오탐 0건
    assert rows["traffic_pedestrian"]["false_rate"] == 100.0


def test_기간_밖_판정은_제외된다(db):
    """since 는 판정 시각이 아니라 **사건 발생 시각**(occurred_at) 기준이다."""
    from datetime import datetime, timedelta, timezone
    old = _ev_typed(db, hazard_type_code="traffic_pedestrian")
    FB.record(db, verdict=VERDICT_TRUE, user=_User(), event=old,
              occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    db.commit()

    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = FB.counts_by_hazard(db, since=since)
    assert "traffic_pedestrian" not in {r["hazard_type_code"] for r in rows}


def test_판정_없는_유형은_아예_나오지_않는다(db):
    """0건을 「오탐 없음」으로 읽으면 안 되므로, 판정이 없으면 행 자체가 없다."""
    rows = FB.counts_by_hazard(db, domain=f"{PFX}없는도메인")
    assert rows == []
