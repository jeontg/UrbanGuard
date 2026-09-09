"""S-98 위험유형별 SOP 관리 — `hazard_sop_map` 을 실제로 읽는다.

## 왜 이 시험들이 있나

표(`hazard_sop_map`)는 만들어 두고 **읽는 코드도 화면도 없었다.** 표만 있으면
아무도 채우지 못하고, 채워지지 않으면 있으나 마나다.

지금 SOP 는 **「도메인 × 등급」** 두 축으로만 고른다. 그래서 「침수」면
지하차도든 배수로든 **같은 체크리스트**가 나온다. 실제로는 조치가 다르다 —
지하차도는 **차량 진입 차단이 최우선**이다.

## 지켜야 할 것

* ★ **연결이 비어 있으면 지금과 완전히 같다.** 여기가 깨지면 매핑을 넣는
  순간이 아니라 **넣지 않은 모든 기관**에서 SOP 가 달라진다
* ★ **더한다. 갈아치우지 않는다.** 매핑을 한 줄 넣었다고 「영상으로 현장
  확인」이 사라지면 그건 지금보다 나쁘다
* ⚠️ **매핑 전용 단계가 기본 매칭에 걸리면 안 된다** — 「침수」 전체에
  지하차도 단계가 붙어 버린다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import events as E
from tot_dashboard.core import sop
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (Event, EventAction, EventSopCheck,
                                       HazardSopMap, SopStep)
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(EventSopCheck))
        s.execute(sa_delete(EventAction))
        s.execute(sa_delete(Event))
        # 매핑이 단계를 참조하므로 먼저 지운다.
        s.execute(sa_delete(HazardSopMap))
        s.execute(sa_delete(SopStep))
        s.commit()
    finally:
        s.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    db = get_session()
    try:
        # 어휘가 먼저다 — 위험유형이 없으면 매핑이 FK 에 걸린다.
        V.seed_builtin(db)
        sop.seed_builtin(db)
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def _event(db, *, domain="flood", level="경계", hazard=""):
    ev = Event(domain=domain, block_id="BLOCK-TEST", place_name="시험지점",
               event_type="침수", level=level, peak_level=level,
               status=E.OPEN, hazard_type_code=hazard)
    db.add(ev)
    db.flush()
    return ev


# --- ★ 비어 있으면 지금과 같다 ----------------------------------------------


def test_매핑이_비면_기존_동작_그대로다():
    """★ 여기가 깨지면 **매핑을 안 넣은 모든 기관**에서 SOP 가 달라진다."""
    db = get_session()
    try:
        ev = _event(db, hazard="flood")
        base = {s.title for s in sop.steps_for(db, "flood", "경계")}
        got = {s.title for s in sop.steps_for_event(db, ev)}
        assert got == base
    finally:
        db.rollback()
        db.close()


def test_위험유형이_비어도_터지지_않는다():
    """이벤트에 유형이 안 붙어 있어도 SOP 는 나와야 한다."""
    db = get_session()
    try:
        ev = _event(db, hazard="")
        assert sop.steps_for_event(db, ev)
    finally:
        db.rollback()
        db.close()


# --- ★ 더한다, 갈아치우지 않는다 --------------------------------------------


def test_세분류_단계가_더해진다():
    db = get_session()
    try:
        sop.seed_hazard_sop(db)
        db.flush()
        general = _event(db, hazard="flood")
        under = _event(db, hazard="flood_underpass")

        g = {s.title for s in sop.steps_for_event(db, general)}
        u = {s.title for s in sop.steps_for_event(db, under)}

        assert "지하차도 진입 차단 우선 검토" in u
        # ★ 일반 침수에는 붙지 않는다.
        assert "지하차도 진입 차단 우선 검토" not in g
        # ★ 갈아치우지 않았다 — 기존 단계가 전부 남아 있다.
        assert g <= u
    finally:
        db.rollback()
        db.close()


def test_공통_단계가_사라지지_않는다():
    """⚠️ 「영상으로 현장 확인」이 빠진 체크리스트는 지금보다 나쁘다."""
    db = get_session()
    try:
        sop.seed_hazard_sop(db)
        db.flush()
        ev = _event(db, hazard="flood_underpass")
        titles = {s.title for s in sop.steps_for_event(db, ev)}
        assert "영상으로 현장 확인" in titles
        assert "상황 일지 기록" in titles
    finally:
        db.rollback()
        db.close()


def test_매핑전용_단계는_기본매칭에_걸리지_않는다():
    """★ 걸리면 「침수」 전체에 지하차도 단계가 붙는다."""
    db = get_session()
    try:
        sop.seed_hazard_sop(db)
        db.flush()
        titles = {s.title for s in sop.steps_for(db, "flood", "경계")}
        assert "지하차도 진입 차단 우선 검토" not in titles
    finally:
        db.rollback()
        db.close()


def test_등급이_다르면_붙지_않는다():
    db = get_session()
    try:
        sop.seed_hazard_sop(db)
        db.flush()
        ev = _event(db, hazard="flood_underpass", level="주의")
        titles = {s.title for s in sop.steps_for_event(db, ev)}
        assert "지하차도 진입 차단 우선 검토" not in titles
    finally:
        db.rollback()
        db.close()


def test_시드는_두_번_돌려도_안_늘어난다():
    """기관이 지운 것을 되살리지 않고, 중복도 만들지 않는다."""
    db = get_session()
    try:
        first = sop.seed_hazard_sop(db)
        db.flush()
        second = sop.seed_hazard_sop(db)
        assert first[0] > 0 and first[1] > 0
        assert second == (0, 0)
    finally:
        db.rollback()
        db.close()


# --- 화면 -------------------------------------------------------------------


def test_화면이_열린다(client):
    r = client.get("/settings/hazard-sop")
    assert r.status_code == 200
    assert "위험유형별 SOP" in r.text


def test_화면이_더해진다고_말한다(client):
    """⚠️ 갈아치우는 줄 알면 기관이 매핑을 무서워서 못 쓴다."""
    r = client.get("/settings/hazard-sop")
    assert "갈아치우지 않습니다" in r.text
    assert "연결이 하나도 없으면" in r.text


def test_연결을_추가하고_지운다(client):
    db = get_session()
    try:
        step = db.scalars(
            __import__("sqlalchemy").select(SopStep)).first()
        sid = step.id
    finally:
        db.close()

    r = client.post("/settings/hazard-sop/add", data={
        "hazard_type_code": "flood_underpass", "level_code": "경계",
        "zone_id": "", "sop_step_id": str(sid), "seq": "10"})
    assert r.status_code == 200
    assert "추가했습니다" in r.text

    db = get_session()
    try:
        row = db.scalars(__import__("sqlalchemy").select(HazardSopMap)).first()
        rid = row.id
    finally:
        db.close()

    r = client.post("/settings/hazard-sop/delete", data={"row_id": str(rid)})
    assert r.status_code == 200
    assert "지웠습니다" in r.text


def test_같은_연결을_두_번_넣지_못한다(client):
    """⚠️ 막지 않으면 유일 제약에 걸려 500 이 난다."""
    db = get_session()
    try:
        sid = db.scalars(__import__("sqlalchemy").select(SopStep)).first().id
    finally:
        db.close()
    data = {"hazard_type_code": "flood_underpass", "level_code": "경계",
            "zone_id": "", "sop_step_id": str(sid), "seq": "10"}
    assert client.post("/settings/hazard-sop/add", data=data).status_code == 200
    r = client.post("/settings/hazard-sop/add", data=data)
    assert r.status_code == 400
    assert "이미 같은 연결" in r.text


def test_없는_위험유형은_거부한다(client):
    db = get_session()
    try:
        sid = db.scalars(__import__("sqlalchemy").select(SopStep)).first().id
    finally:
        db.close()
    r = client.post("/settings/hazard-sop/add", data={
        "hazard_type_code": "없는유형", "level_code": "",
        "zone_id": "", "sop_step_id": str(sid), "seq": "0"})
    assert r.status_code == 400
    assert "없는 위험유형" in r.text


def test_단계를_안_고르면_거부한다(client):
    r = client.post("/settings/hazard-sop/add", data={
        "hazard_type_code": "flood_underpass", "level_code": "",
        "zone_id": "", "sop_step_id": "0", "seq": "0"})
    assert r.status_code == 400
    assert "SOP 단계를 고르십시오" in r.text


def test_구역이_없으면_화면이_그렇다고_말한다(client):
    """⚠️ 「도, 시·군별」(경남 SFR-012)을 못 쓰는 상태임을 드러낸다."""
    r = client.get("/settings/hazard-sop")
    assert "등록된 구역이 없어" in r.text
