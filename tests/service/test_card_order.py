"""실시간 관제 화면 정렬 (S-85 설정 → S-20 화면).

**왜 있는 기능인가.** 화면이 몇 초마다 갱신되는데 카드가 위험도순으로 다시
정렬되므로, 값이 흔들릴 때마다 지점 카드가 자리를 옮긴다. 관제 중 읽고 있던
지점이 화면 밖으로 밀리는 일이 생긴다.

지켜야 할 것.

* **기본값은 그대로** — 위험도순은 위험한 지점을 위로 올려 주는 이점이 있다.
  없애지 않고 고를 수 있게만 한다
* **고정을 골라도 지점이 빠지지 않는다** — 목록에서 사라지는 것이 자리
  이동보다 훨씬 나쁘다
* **모르는 값은 기본값으로 눕힌다** — 화면은 목록에서만 고르게 하므로 다른
  값이 왔다면 직접 만든 요청이다
* **차량 목록 순서를 바꿔도 표시 대상은 그대로** — 서버가 감속 큰 차량을
  고르고, 설정은 줄 세우는 순서만 정한다
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AppSetting, AuditLog
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
        s.execute(sa_delete(AppSetting))
        s.execute(sa_delete(AuditLog))
        s.commit()
    finally:
        s.close()
    S.invalidate()


def _save(client, **over):
    """S-85 저장. 이 화면은 한 폼에서 전부 저장하므로 필수 칸을 함께 보낸다."""
    data = {"org_name": "테스트시", "solution_name": "통합 도시안전 관제",
            "board_bg": "#0F1420", "map_max_zoom": "18"}
    data.update(over)
    return client.post("/settings/org", data=data)


# --- 설정 -------------------------------------------------------------------

def test_기본값은_지금까지의_동작이다():
    """바꾸고 싶은 기관만 바꾼다 — 기본 동작을 조용히 뒤집지 않는다."""
    assert S.DEFAULTS[S.KEY_CARD_ORDER] == "severity"
    assert S.DEFAULTS[S.KEY_OBJECT_ORDER] == "drop"


def test_선택지에_고정이_있다():
    assert "fixed" in S.CARD_ORDERS and "name" in S.CARD_ORDERS
    assert "id" in S.OBJECT_ORDERS
    # 각 선택지에 라벨과 설명이 함께 있어야 화면이 무엇인지 말해 줄 수 있다.
    for label, desc in list(S.CARD_ORDERS.values()) + list(S.OBJECT_ORDERS.values()):
        assert label and desc


def test_설정_화면에_정렬_항목이_있다(client):
    r = client.get("/settings/org")
    assert r.status_code == 200
    assert "실시간 관제 화면 정렬" in r.text
    assert 'name="card_order"' in r.text and 'name="object_order"' in r.text
    # 왜 필요한 설정인지가 화면에 적혀 있어야 한다.
    assert "읽고 있던 지점을" in r.text


def test_저장하면_반영되고_감사로그가_남는다(client):
    r = _save(client, card_order="fixed", object_order="id")
    assert r.status_code == 200

    db = get_session()
    try:
        assert S.get(S.KEY_CARD_ORDER, db) == "fixed"
        assert S.get(S.KEY_OBJECT_ORDER, db) == "id"
        rows = db.scalars(__import__("sqlalchemy").select(AuditLog)).all()
        assert rows and rows[-1].after.get("card_order") == "fixed"
    finally:
        db.close()


def test_모르는_값은_기본값으로_눕는다(client):
    r = _save(client, card_order="랜덤", object_order="무작위")
    assert r.status_code == 200
    db = get_session()
    try:
        assert S.get(S.KEY_CARD_ORDER, db) == "severity"
        assert S.get(S.KEY_OBJECT_ORDER, db) == "drop"
    finally:
        db.close()


# --- 화면 전달 ---------------------------------------------------------------

def _injected(text: str) -> dict:
    """index.html 이 화면 스크립트에 넘긴 값을 읽어 온다."""
    out = {}
    for key in ("UG_CARD_ORDER", "UG_OBJECT_ORDER"):
        m = re.search(rf'window\.{key} = "([^"]*)"', text)
        out[key] = m.group(1) if m else None
    return out


def test_관제_화면에_설정이_전달된다(client):
    _save(client, card_order="fixed", object_order="id")
    r = client.get("/flood")
    assert r.status_code == 200
    got = _injected(r.text)
    assert got["UG_CARD_ORDER"] == "fixed"
    assert got["UG_OBJECT_ORDER"] == "id"


def test_기본값도_그대로_전달된다(client):
    r = client.get("/flood")
    assert r.status_code == 200
    got = _injected(r.text)
    assert got["UG_CARD_ORDER"] == "severity"
    assert got["UG_OBJECT_ORDER"] == "drop"


def test_화면_스크립트에_정렬_함수가_있다():
    """설정만 있고 화면이 안 쓰면 아무 일도 안 일어난다."""
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
          / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function orderBlocks(" in js
    # 그리기는 renderBoard 한 곳으로 모았다(폴링·SSE 공용). 정렬은 거기서 건다.
    assert "function renderBoard(" in js
    assert "blocks = orderBlocks(blocks" in js, (
        "renderBoard 가 정렬을 걸지 않으면 설정이 무시된다")
    # **두 경로 모두** renderBoard 를 지나야 한다. 한쪽만 지나면 그쪽에서만
    # 정렬이 안 걸리는, 찾기 어려운 상태가 된다.
    assert js.count("renderBoard(") >= 3, (
        "renderBoard 정의 + 폴링 + SSE 세 군데에서 보여야 한다")
    # 옛 무조건 정렬이 남아 있으면 설정이 무시된다.
    assert "(data.blocks || []).sort((a, b) => b.severity - a.severity)" not in js
    # 「고정」은 등록순(INITIAL_BLOCKS)을 기준으로 한다.
    assert "INITIAL_BLOCKS" in js and "UG_CARD_ORDER" in js
    assert "UG_OBJECT_ORDER" in js


def test_관제요원은_설정을_바꿀_수_없다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        r = _save(anon_client, card_order="fixed")
        assert r.status_code in (302, 303, 403)
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])

    db = get_session()
    try:
        assert S.get(S.KEY_CARD_ORDER, db) == "severity"
    finally:
        db.close()
