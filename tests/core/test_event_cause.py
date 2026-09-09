"""이벤트 상세 — 「발생 원인」 설명 (2026-09-03 신설).

지켜야 할 것.

* 도메인마다 다른 ``detail`` 키 모양에서도 사람이 읽을 문장이 나온다.
* 판정 기준표(``vocabulary.threshold_rows``)가 있으면 그 등급을 만든
  기준 행을 정확히 짚어 준다.
* 모르는 모양·예외 상황에서도 **절대 죽지 않는다** — 일반 문장으로
  물러난다.
* 새 컬럼·마이그레이션 없이, 이미 저장된 값만으로 만들어진다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import event_cause as EC
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.models import Event


@pytest.fixture
def db(db_schema):
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import Event, HazardType, LevelThreshold
    s = get_session()
    s.query(Event).delete()
    s.commit()
    V.seed_builtin(s)
    s.commit()
    yield s
    s.query(Event).delete()
    s.commit()
    s.close()


def _ev(**kw) -> Event:
    base = dict(domain="flood", block_id="B", place_name="테스트지점",
               event_type="침수", level="주의", peak_level="주의", status="open")
    base.update(kw)
    return Event(**base)


def test_침수_이벤트는_침수심과_기준을_함께_보여준다(db):
    ev = _ev(domain="flood", event_type="침수", hazard_type_code="flood_underpass",
             level="주의", detail={"추정 침수심(cm)": 7, "물 비율": 0.12})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert out["hazard_label"] == "지하차도 침수"
    assert "침수심 7cm" in out["headline"]
    assert "물 비율 12%" in out["headline"]
    assert "5cm 이상" in out["headline"]  # 「주의」 기준
    assert out["matched_threshold"]["label"] == "주의"
    assert len(out["threshold_rows"]) == 3  # 주의·경계·심각


def test_교통_돌발상황은_근거_문장을_그대로_쓴다(db):
    ev = _ev(domain="traffic", event_type="역주행 의심",
             hazard_type_code="traffic_wrongway", level="경계",
             detail={"근거": "진행방향 역행 2.3초 지속", "판정": "wrongway"})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert out["hazard_label"] == "역주행 의심"
    assert "진행방향 역행 2.3초 지속" in out["headline"]
    # 교통은 다변량 판정이라 기준표 자체가 없다(vocabulary.py 설계 확정).
    assert out["threshold_rows"] == []
    assert out["matched_threshold"] is None


def test_교통_TWR_경로는_강수_속도로_설명한다(db):
    ev = _ev(domain="traffic", event_type="교통위험", level="주의",
             detail={"강수(mm/h)": 12, "강수 강도": "보통", "평균속도(km/h)": 18,
                     "판정코드": "TWR_RAIN_CONGESTION"})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert "강수 12mm/h(보통)" in out["headline"]
    assert "평균속도 18km/h" in out["headline"]


def test_인파_밀집_이벤트는_근거_태그를_보여준다(db):
    ev = _ev(domain="crowd", event_type="인파관리", level="경계",
             hazard_type_code="crowd_density",
             detail={"판정": "밀집 위험", "위험도 점수": 3, "현재 인원": 42,
                     "밀집지수": 4.2, "근거": "밀집도UP, 속도급증"})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert "밀집도UP, 속도급증" in out["headline"]
    assert out["matched_threshold"]["label"] == "경계"


def test_노면_손상_이벤트는_유형과_건수를_보여준다(db):
    ev = _ev(domain="road", event_type="노면관리", level="주의",
             hazard_type_code="road_pothole",
             detail={"손상 유형": "포트홀", "탐지 건수": 3, "노면 등급": "보수 필요"})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert "포트홀" in out["headline"]
    assert "3건" in out["headline"]


def test_주민제보로_전환된_노면_이벤트도_설명이_나온다(db):
    ev = _ev(domain="road", event_type="노면관리", level="주의",
             detail={"제보 번호": 12, "제보자": "opr1", "설명": "포트홀이 큽니다"})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert "주민 제보" in out["headline"]
    assert "포트홀이 큽니다" in out["headline"]


def test_detail이_없어도_안_죽는다(db):
    """detail=None(구버전 이벤트 등)이어도 예외 없이 일반 문장을 낸다."""
    ev = _ev(domain="flood", level="주의", detail=None)
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert ev.level in out["headline"]


def test_알수없는_도메인이어도_안_죽는다(db):
    ev = _ev(domain="unknown-domain", event_type="?", level="주의", detail={"x": 1})
    db.add(ev)
    db.flush()
    out = EC.explain(db, ev)
    assert "주의" in out["headline"]
    assert out["threshold_rows"] == []


# --- 화면 렌더링(S-03) — 함수 값이 실제 템플릿에 그대로 나오는가 -----------
from fastapi.testclient import TestClient  # noqa: E402

from tot_dashboard.service.main import app  # noqa: E402


@pytest.fixture
def admin_client(db, seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


def test_상세화면에_발생원인_카드가_뜬다(admin_client, db):
    ev = _ev(domain="flood", event_type="침수", hazard_type_code="flood_underpass",
             level="주의", detail={"추정 침수심(cm)": 7, "물 비율": 0.12})
    db.add(ev)
    db.commit()

    r = admin_client.get(f"/events/{ev.id}")
    assert r.status_code == 200
    assert "발생 원인" in r.text
    assert "지하차도 침수" in r.text
    assert "침수심 7cm" in r.text
    assert "5cm 이상" in r.text  # 판정 기준표


def test_발생원인_카드는_기준표_없어도_안_깨진다(admin_client, db):
    """교통은 threshold_rows가 비어 있다 — 표 없이도 화면이 정상이어야 한다."""
    ev = _ev(domain="traffic", event_type="역주행 의심",
             hazard_type_code="traffic_wrongway", level="경계",
             detail={"근거": "진행방향 역행 2.3초 지속"})
    db.add(ev)
    db.commit()

    r = admin_client.get(f"/events/{ev.id}")
    assert r.status_code == 200
    assert "발생 원인" in r.text
    assert "진행방향 역행 2.3초 지속" in r.text
