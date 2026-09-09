"""이벤트 생성·중복 억제·조치 (S-02 / S-03).

핵심은 **같은 지점의 위험이 이어지는 동안 이벤트가 하나만 유지되는가**다.
관측마다 이벤트가 새로 생기면 큐가 쓸모없어진다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tot_dashboard.core import events as E


@pytest.fixture
def db(db_schema):
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import Event, EventAction
    s = get_session()
    s.query(EventAction).delete()
    s.query(Event).delete()
    s.commit()
    yield s
    s.close()


def _detect(db, level, block="BLOCK-A", domain="flood"):
    ev = E.record_detection(db, domain=domain, block_id=block,
                            place_name="테스트교차로", level=level)
    db.commit()
    return ev


# --- 생성 기준 ---------------------------------------------------------------
def test_low_level_does_not_create_event(db):
    """「관심」까지 이벤트가 되면 평상시에도 큐가 가득 찬다."""
    assert _detect(db, "관심") is None
    assert E.list_events(db) == []


@pytest.mark.parametrize("level", ["주의", "경계", "심각"])
def test_reportable_levels_create_event(db, level):
    ev = _detect(db, level)
    assert ev is not None
    assert ev.status == E.OPEN
    assert ev.level == level


def test_detection_records_initial_action(db):
    ev = _detect(db, "주의")
    assert [a.action for a in ev.actions] == [E.ACT_DETECTED]


# --- 중복 억제 ---------------------------------------------------------------
def test_repeated_detection_updates_instead_of_duplicating(db):
    first = _detect(db, "주의")
    again = _detect(db, "주의")
    assert again.id == first.id
    assert len(E.list_events(db)) == 1


def test_level_rise_is_recorded_on_the_same_event(db):
    ev = _detect(db, "주의")
    _detect(db, "경계")
    db.refresh(ev)
    assert ev.level == "경계"
    assert ev.peak_level == "경계"
    assert E.ACT_LEVEL_UP in [a.action for a in ev.actions]


def test_level_drop_keeps_peak_and_does_not_close(db):
    """등급이 내려가도 자동으로 닫지 않는다 — 종결은 사람이 판단할 일이다."""
    ev = _detect(db, "심각")
    _detect(db, "주의")
    db.refresh(ev)
    assert ev.level == "주의"
    assert ev.peak_level == "심각"
    assert ev.status in E.ACTIVE


def test_different_blocks_get_separate_events(db):
    _detect(db, "주의", block="BLOCK-A")
    _detect(db, "주의", block="BLOCK-B")
    assert len(E.list_events(db)) == 2


def test_closed_event_does_not_block_a_new_one(db):
    first = _detect(db, "주의")
    E.close(db, first, None)
    db.commit()
    second = _detect(db, "주의")
    assert second is not None and second.id != first.id


# --- 조치 -------------------------------------------------------------------
def test_acknowledge_moves_to_in_progress(db):
    ev = _detect(db, "주의")
    E.acknowledge(db, ev, None, "현장 확인 요청")
    db.commit()
    assert ev.status == E.IN_PROGRESS


def test_close_sets_closed_at(db):
    ev = _detect(db, "주의")
    E.close(db, ev, None)
    db.commit()
    assert ev.status == E.CLOSED and ev.closed_at is not None


def test_false_positive_marks_and_closes(db):
    """오탐 신고는 종결까지 함께 처리한다 — 재학습 데이터로 남는다."""
    ev = _detect(db, "경계")
    E.mark_false_positive(db, ev, None, "그림자를 물로 오인")
    db.commit()
    assert ev.false_positive is True
    assert ev.status == E.CLOSED


# --- 조회 -------------------------------------------------------------------
def test_domain_scope_filters_events(db):
    _detect(db, "주의", block="F1", domain="flood")
    _detect(db, "주의", block="R1", domain="road")
    only_road = E.list_events(db, allowed_domains={"road"})
    assert [e.domain for e in only_road] == ["road"]


def test_empty_scope_returns_nothing(db):
    """담당 도메인이 하나도 없는 계정에게 전체가 보이면 안 된다."""
    _detect(db, "주의")
    assert E.list_events(db, allowed_domains=set()) == []


def test_counts_by_status(db):
    a = _detect(db, "주의", block="A")
    _detect(db, "주의", block="B")
    E.close(db, a, None)
    db.commit()
    c = E.counts(db)
    assert c[E.OPEN] == 1 and c[E.CLOSED] == 1


def test_elapsed_text_is_human_readable(db):
    ev = _detect(db, "주의")
    assert E.elapsed_text(ev) in ("방금", "1분")


# --- split_by_hazard_type (2026-08-26, 교통 돌발상황 확장) ------------------
#
# 같은 카메라에서 동시에 일어날 수 있는 독립 사건(보행자·역주행·사고 의심)이
# 서로를 덮어쓰면 안 된다. 다만 기존 도메인(침수·인파·노면, 그리고 유형을
# 나누지 않는 기존 교통 호출부)의 「(도메인, 지점) 당 이벤트 1건」 동작은
# 절대 바뀌면 안 된다 — `test_repeated_detection_updates_instead_of_duplicating`
# 가 이미 그 계약을 지키고 있다.

def test_기본값에서는_유형이_달라도_같은_이벤트로_본다(db):
    """split_by_hazard_type 기본값(False) — 예전과 동일하게 (도메인, 지점)만
    본다. 유형이 달라도 새 이벤트를 만들지 않고 기존 이벤트를 찾는다."""
    first = E.record_detection(db, domain="traffic", block_id="BLOCK-A",
                               place_name="교차로", level="경계",
                               hazard_type_code="traffic_rain_congestion")
    db.commit()
    second = E.record_detection(db, domain="traffic", block_id="BLOCK-A",
                                place_name="교차로", level="경계",
                                hazard_type_code="traffic_stalled_vehicle")
    db.commit()
    assert second.id == first.id
    # 처음 붙은 유형이 유지된다 — 나중 탐지가 이미 있는 유형을 덮지 않는다
    # (record_detection 의 갱신 경로는 hazard_type_code 를 건드리지 않는다).
    assert second.hazard_type_code == "traffic_rain_congestion"


def test_split_by_hazard_type을_주면_유형별로_별도_이벤트가_된다(db):
    """★ 이 계약이 없으면 같은 카메라의 보행자·역주행이 한 이벤트에서
    서로를 덮어쓴다."""
    pedestrian = E.record_detection(
        db, domain="traffic", block_id="BLOCK-A", place_name="교차로",
        level="주의", hazard_type_code="traffic_pedestrian",
        split_by_hazard_type=True)
    wrongway = E.record_detection(
        db, domain="traffic", block_id="BLOCK-A", place_name="교차로",
        level="경계", hazard_type_code="traffic_wrongway",
        split_by_hazard_type=True)
    db.commit()
    assert pedestrian is not None and wrongway is not None
    assert pedestrian.id != wrongway.id
    assert pedestrian.hazard_type_code == "traffic_pedestrian"
    assert wrongway.hazard_type_code == "traffic_wrongway"


def test_split_by_hazard_type이어도_같은_유형은_갱신된다(db):
    """유형을 나눠도, 같은 유형이 다시 탐지되면 새로 만들지 않고 갱신한다."""
    first = E.record_detection(
        db, domain="traffic", block_id="BLOCK-A", place_name="교차로",
        level="주의", hazard_type_code="traffic_wrongway",
        split_by_hazard_type=True)
    db.commit()
    second = E.record_detection(
        db, domain="traffic", block_id="BLOCK-A", place_name="교차로",
        level="경계", hazard_type_code="traffic_wrongway",
        split_by_hazard_type=True)
    db.commit()
    assert second.id == first.id
    assert second.level == "경계"


# --- 자동 보류 (2026-09-02 신설) ---------------------------------------------
#
# ⚠️ 핵심 안전장치: 이 정책은 **종결이 아니다** — `close()`를 절대 호출하지
# 않는다. `test_level_drop_keeps_peak_and_does_not_close`가 이미 지키고
# 있는 "등급이 내려가도 자동으로 닫지 않는다" 원칙을 이 정책도 그대로
# 지켜야 한다.

def test_생성_시점에_last_detected_at이_채워진다(db):
    ev = _detect(db, "주의")
    assert ev.last_detected_at is not None


def test_임계등급_미만_재탐지는_last_detected_at을_안_바꾼다(db):
    """"관심"으로의 재탐지나 20초 하트비트성 갱신까지 "재탐지"로 치면
    자동 보류 판정이 무의미해진다."""
    ev = _detect(db, "경계")
    first_seen = ev.last_detected_at
    _detect(db, "관심")  # 임계(주의) 미만 — 기존 이벤트를 갱신은 하되
    db.refresh(ev)
    assert ev.level == "관심"           # 갱신은 됐지만
    assert ev.last_detected_at == first_seen  # 재탐지 시각은 그대로


def test_임계등급_이상_재탐지는_last_detected_at을_갱신한다(db):
    ev = _detect(db, "주의")
    first_seen = ev.last_detected_at
    ev.last_detected_at = first_seen - timedelta(hours=1)  # 시간을 벌린다
    db.commit()
    _detect(db, "경계")  # 임계 이상
    db.refresh(ev)
    assert ev.last_detected_at > first_seen - timedelta(hours=1)


def _age(db, ev, hours: float) -> None:
    """시험 편의 — 재탐지가 `hours`시간 전에 멈춘 것처럼 만든다."""
    ev.last_detected_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    db.commit()


def test_기준시간_넘게_방치된_이벤트만_자동_보류로_옮긴다(db):
    stale = _detect(db, "주의", block="OLD")
    fresh = _detect(db, "주의", block="NEW")
    _age(db, stale, 25)
    moved = E.auto_hold_stale(db, threshold_hours=24)
    db.commit()
    assert [e.id for e in moved] == [stale.id]
    db.refresh(stale)
    db.refresh(fresh)
    assert stale.status == E.AUTO_HELD
    assert fresh.status == E.OPEN  # 최근 것은 그대로 둔다


def test_자동_보류는_종결이_아니다(db):
    """★ 핵심 안전장치 — peak_level·closed_at 등 종결의 흔적이 없어야
    한다. `close()`를 호출하지 않았다는 뜻이다."""
    ev = _detect(db, "심각")
    _age(db, ev, 25)
    E.auto_hold_stale(db, threshold_hours=24)
    db.commit()
    db.refresh(ev)
    assert ev.status == E.AUTO_HELD
    assert ev.status != E.CLOSED
    assert ev.closed_at is None
    assert E.ACT_AUTO_HOLD in [a.action for a in ev.actions]


def test_이미_종결된_이벤트는_자동_보류_대상이_아니다(db):
    ev = _detect(db, "주의")
    E.close(db, ev, None)
    db.commit()
    _age(db, ev, 100)
    moved = E.auto_hold_stale(db, threshold_hours=24)
    assert moved == []


def test_자동_보류_후_재탐지되면_새_이벤트가_열린다(db):
    """`CLOSED` 이벤트에 재탐지가 오면 새 이벤트가 열리는 기존 동작
    (`test_closed_event_does_not_block_a_new_one`)과 같은 패턴이어야
    한다 — `AUTO_HELD`는 `ACTIVE`에 없으므로 `open_event_for()`가
    못 찾는다."""
    first = _detect(db, "주의")
    _age(db, first, 25)
    E.auto_hold_stale(db, threshold_hours=24)
    db.commit()
    second = _detect(db, "주의")
    assert second is not None and second.id != first.id


def test_counts에_자동_보류_항목이_있다(db):
    ev = _detect(db, "주의")
    _age(db, ev, 25)
    E.auto_hold_stale(db, threshold_hours=24)
    db.commit()
    c = E.counts(db)
    assert c[E.AUTO_HELD] == 1
