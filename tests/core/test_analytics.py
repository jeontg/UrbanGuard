"""통계·성과 리포트(S-60)와 모델 운영(S-61).

이 숫자가 지자체 성과 보고와 다음 해 예산 근거가 되므로, 계산이 틀리면
곤란해지는 쪽은 고객이다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tot_dashboard.core import analytics as A
from tot_dashboard.core import events as E
from tot_dashboard.core import roles as R


@pytest.fixture
def db(db_schema):
    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import (CitizenReport, DetectionFeedback,
                                           Event, EventAction, Notification,
                                           User)
    s = get_session()
    for m in (DetectionFeedback, Notification, CitizenReport, EventAction, Event):
        s.query(m).delete()
    s.query(User).filter(User.login_id.like("an_%")).delete(synchronize_session=False)
    s.commit()
    create_user(s, login_id="an_opr", name="an_opr", dept="테스트",
                role=R.Role.OPR.value, password="AnalyticsT!2026",
                domains=[d.value for d in R.Domain], must_change=False)
    s.commit()
    yield s
    for m in (DetectionFeedback, Notification, CitizenReport, EventAction, Event):
        s.query(m).delete()
    s.query(User).filter(User.login_id.like("an_%")).delete(synchronize_session=False)
    s.commit()
    s.close()


@pytest.fixture
def user(db):
    from tot_dashboard.core.models import User
    return db.query(User).filter(User.login_id == "an_opr").one()


def _ev(db, domain="flood", level="주의", block="B1", days_ago=0):
    ev = E.record_detection(db, domain=domain, block_id=block,
                            place_name=block, level=level)
    if days_ago:
        ev.detected_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.flush()
    return ev


# --- 기본 집계 ---------------------------------------------------------------
def test_empty_period_does_not_divide_by_zero(db):
    s = A.summary(db)
    assert s["total"] == 0 and s["close_rate"] == 0.0
    assert s["false_positive_rate"] == 0.0


def test_counts_and_close_rate(db, user):
    a = _ev(db, block="B1")
    _ev(db, block="B2")
    E.close(db, a, user)
    db.commit()
    s = A.summary(db)
    assert s["total"] == 2 and s["closed"] == 1 and s["open"] == 1
    assert s["close_rate"] == 50.0


def test_false_positive_rate(db, user):
    a = _ev(db, block="B1")
    _ev(db, block="B2")
    _ev(db, block="B3")
    _ev(db, block="B4")
    E.mark_false_positive(db, a, user)
    db.commit()
    assert A.summary(db)["false_positive_rate"] == 25.0


def test_events_outside_the_period_are_excluded(db):
    _ev(db, block="OLD", days_ago=100)
    _ev(db, block="NEW")
    db.commit()
    assert A.summary(db, "7d")["total"] == 1
    assert A.summary(db, "365d")["total"] == 2


# --- 최고 등급 기준 -----------------------------------------------------------
def test_level_breakdown_uses_peak_not_current(db):
    """등급이 내려간 건도 「경계까지 갔던 건」으로 세야 대응 적정성을 본다."""
    E.record_detection(db, domain="flood", block_id="B1",
                       place_name="B1", level="경계")
    E.record_detection(db, domain="flood", block_id="B1",
                       place_name="B1", level="주의")
    db.commit()
    levels = {x["level"]: x["count"] for x in A.summary(db)["by_level"]}
    assert levels == {"경계": 1}


def test_domain_breakdown(db):
    _ev(db, domain="flood", block="F1")
    _ev(db, domain="flood", block="F2")
    _ev(db, domain="road", block="R1")
    db.commit()
    counts = {d["domain"]: d["count"] for d in A.summary(db)["by_domain"]}
    assert counts == {"flood": 2, "road": 1}


# --- 대응 소요시간 -----------------------------------------------------------
def test_response_time_measures_first_acknowledge(db, user):
    ev = _ev(db)
    ev.detected_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.flush()
    E.acknowledge(db, ev, user)
    db.commit()
    s = A.summary(db)
    assert s["ack_count"] == 1
    assert 29 <= s["ack_avg_min"] <= 31


def test_unacknowledged_events_are_not_counted(db):
    _ev(db)
    db.commit()
    s = A.summary(db)
    assert s["ack_count"] == 0 and s["ack_avg_min"] is None


def test_only_the_first_acknowledge_counts(db, user):
    """확인을 두 번 눌러도 소요시간이 두 번 잡히면 안 된다."""
    ev = _ev(db)
    E.acknowledge(db, ev, user)
    E.acknowledge(db, ev, user)
    db.commit()
    assert A.summary(db)["ack_count"] == 1


# --- 도메인 범위 -------------------------------------------------------------
def test_domain_scope_limits_the_summary(db):
    _ev(db, domain="flood", block="F1")
    _ev(db, domain="road", block="R1")
    db.commit()
    assert A.summary(db, allowed_domains={"road"})["total"] == 1
    assert A.summary(db, allowed_domains=set())["total"] == 0


# --- 일자별 추이 -------------------------------------------------------------
def test_daily_counts_fill_empty_days(db):
    _ev(db)
    db.commit()
    daily = A.daily_counts(db, "7d")
    assert len(daily) == 7
    assert sum(d["count"] for d in daily) == 1


def test_daily_bucket_uses_utc_not_the_db_server_timezone(db):
    """PostgreSQL 은 timestamptz 를 서버 시간대(Asia/Seoul)로 돌려준다.

    UTC 기준 날짜 키와 섞이면 자정 근처 건이 하루 어긋나 통계가 밀린다.
    실제로 발생했던 결함이라 회귀 방지로 남긴다.
    """
    _ev(db)
    db.commit()
    daily = A.daily_counts(db, "7d")
    today = datetime.now(timezone.utc).strftime("%m-%d")
    hit = next(d for d in daily if d["date"] == today)
    assert hit["count"] == 1


def test_long_period_is_capped_for_readability(db):
    """1년치 막대를 다 그리면 읽히지 않는다."""
    assert len(A.daily_counts(db, "365d")) == 60


# --- 모델 운영 ---------------------------------------------------------------
def test_model_stats_cover_every_domain(db):
    """★ 2026-08-22 전수점검 — MODEL_DOMAINS 에 traffic 이 빠져 있어
    /models 현황표에서 교통 이벤트 탐지·오탐 집계가 통째로 빠지고 있었다."""
    rows = A.model_stats(db)
    assert {r["domain"] for r in rows} == {"flood", "traffic", "crowd", "road"}


def test_model_fp_rate_is_none_without_detections(db):
    rows = {r["domain"]: r for r in A.model_stats(db)}
    assert rows["road"]["detected"] == 0
    assert rows["road"]["fp_rate"] is None


def test_model_fp_rate_counts_reported_false_positives(db, user):
    a = _ev(db, domain="road", block="R1")
    _ev(db, domain="road", block="R2")
    E.mark_false_positive(db, a, user)
    db.commit()
    rows = {r["domain"]: r for r in A.model_stats(db)}
    assert rows["road"]["detected"] == 2 and rows["road"]["fp_rate"] == 50.0


def test_training_data_status_counts_only_usable_photos(db, user):
    from tot_dashboard.core.models import CitizenReport
    db.add(CitizenReport(domain="road", photo_path="a.jpg",
                         usable_for_training=True))
    db.add(CitizenReport(domain="road", photo_path="b.jpg",
                         usable_for_training=False))
    db.add(CitizenReport(domain="road", photo_path="",
                         usable_for_training=True))   # 사진 파기됨
    db.commit()
    st = A.training_data_status(db)
    assert st["usable"] == 1 and st["total"] == 3


# --- 유형별 오탐률 (Phase 3, 2026-08-26) --------------------------------------
#
# ★ 백엔드를 새로 만들지 않는다 — feedback.record() 가 event.hazard_type_code
#   를 이미 굳혀 왔으므로, detection_quality() 는 그 판정을 유형 단위로
#   묶어 노출만 한다(docs/202608260842/ 계획 Phase 3).


def _typed_ev(db, *, domain="traffic", hazard_type_code="traffic_pedestrian",
             block="T1"):
    return E.record_detection(
        db, domain=domain, block_id=block, place_name=block, level="주의",
        hazard_type_code=hazard_type_code, split_by_hazard_type=True)


def test_판정_없으면_표가_비어_있다(db):
    """0건을 「오탐 없음」으로 보이면 안 되므로, 판정이 없는 유형은
    아예 목록에 나오지 않아야 한다."""
    _typed_ev(db, hazard_type_code="traffic_pedestrian")
    db.commit()
    assert A.detection_quality(db) == []


def test_유형별_오탐률이_노출된다(db, user):
    from tot_dashboard.core import feedback as FB
    from tot_dashboard.core import vocabulary as V
    from tot_dashboard.core.models import VERDICT_FALSE, VERDICT_TRUE

    V.seed_builtin(db)  # 실서비스는 기동 시 이미 심어져 있다 — 라벨 조회용
    ev1 = _typed_ev(db, hazard_type_code="traffic_pedestrian", block="T1")
    ev2 = _typed_ev(db, hazard_type_code="traffic_rain_congestion", block="T2")
    db.commit()
    FB.record(db, verdict=VERDICT_FALSE, user=user, event=ev1, reason="그림자")
    FB.record(db, verdict=VERDICT_TRUE, user=user, event=ev2)
    db.commit()

    rows = {r["hazard_type_code"]: r for r in A.detection_quality(db)}
    assert rows["traffic_pedestrian"]["false_rate"] == 100.0
    assert rows["traffic_pedestrian"]["label"] == "보행자 도로 진입"
    assert rows["traffic_rain_congestion"]["false_rate"] == 0.0


def test_표본이_적으면_low_sample이_켜진다(db, user):
    from tot_dashboard.core import feedback as FB
    from tot_dashboard.core.models import VERDICT_TRUE

    ev = _typed_ev(db, hazard_type_code="traffic_pedestrian")
    db.commit()
    FB.record(db, verdict=VERDICT_TRUE, user=user, event=ev)
    db.commit()

    rows = {r["hazard_type_code"]: r for r in A.detection_quality(db)}
    assert rows["traffic_pedestrian"]["judged"] == 1
    assert rows["traffic_pedestrian"]["low_sample"] is True  # < MIN_SAMPLE(20)


def test_도메인_범위가_유형별_오탐률에도_적용된다(db, user):
    from tot_dashboard.core import feedback as FB
    from tot_dashboard.core.models import VERDICT_TRUE

    traffic_ev = _typed_ev(db, domain="traffic",
                           hazard_type_code="traffic_pedestrian", block="T1")
    road_ev = _typed_ev(db, domain="road", hazard_type_code="road_pothole",
                        block="T2")
    db.commit()
    FB.record(db, verdict=VERDICT_TRUE, user=user, event=traffic_ev)
    FB.record(db, verdict=VERDICT_TRUE, user=user, event=road_ev)
    db.commit()

    rows = A.detection_quality(db, allowed_domains={"road"})
    assert {r["domain"] for r in rows} == {"road"}


def test_기간_밖_판정은_유형별_오탐률에서도_빠진다(db, user):
    from datetime import datetime, timedelta, timezone

    from tot_dashboard.core import feedback as FB
    from tot_dashboard.core.models import VERDICT_TRUE

    ev = _typed_ev(db, hazard_type_code="traffic_pedestrian")
    db.commit()
    FB.record(db, verdict=VERDICT_TRUE, user=user, event=ev,
              occurred_at=datetime.now(timezone.utc) - timedelta(days=40))
    db.commit()

    rows = A.detection_quality(db, period="7d")
    assert "traffic_pedestrian" not in {r["hazard_type_code"] for r in rows}
