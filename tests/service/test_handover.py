"""교대 인수인계 (S-04).

지켜야 할 것.

* **미처리 이벤트가 자동으로 붙는다** — 손으로 옮겨 적으면 반드시 빠지고,
  빠지는 것은 대체로 「별일 아니라고 생각한 것」이다
* **스냅샷으로 굳는다** — 나중에 이벤트가 종결돼도 「인계 시점에 무엇이 열려
  있었나」가 남아야 한다. 사후 검토는 그 시점의 사실을 묻는다
* **넘긴 뒤에는 고칠 수 없다** — 내용이 나중에 바뀌면 인수자가 확인한 것이
  무엇인지 알 수 없다
* **인계자 본인은 인수 확인할 수 없다** — 혼자 넘기고 혼자 받으면 인수인계가
  아니다
* **지울 길이 없다** — 「전달 못 받았다」는 다툼에 답할 근거다
* **관제요원이 주체다** — 작성과 인수 확인을 둘 다 할 수 있다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import events as E
from tot_dashboard.core import handover as H
from tot_dashboard.core import roles as R
from tot_dashboard.core import sop
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (AuditLog, Event, EventAction,
                                       EventSopCheck, ShiftHandover, SopStep,
                                       User)
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
        s.execute(sa_delete(ShiftHandover))
        s.execute(sa_delete(EventSopCheck))
        s.execute(sa_delete(EventAction))
        s.execute(sa_delete(Event))
        s.execute(sa_delete(SopStep))
        s.execute(sa_delete(AuditLog))
        s.commit()
    finally:
        s.close()


def _event(db, *, place="야간침수지점", level="경계") -> Event:
    ev = Event(domain="flood", block_id="BLOCK-TEST", place_name=place,
               event_type="침수", level=level, peak_level=level, status=E.OPEN)
    db.add(ev)
    db.flush()
    return ev


def _user(db, login_id: str) -> User:
    return db.scalars(select(User).where(User.login_id == login_id)).one()


# --- 스냅샷 -----------------------------------------------------------------

def test_열린_이벤트가_자동으로_붙는다(seeded_users):
    db = get_session()
    try:
        _event(db, place="A지점")
        _event(db, place="B지점")
        closed = _event(db, place="종결지점")
        closed.status = E.CLOSED
        db.commit()

        row = H.create(db, shift_name="야간", summary="특이사항 없음")
        db.commit()
        places = {i["place"] for i in H.open_items(row)}
        assert places == {"A지점", "B지점"}   # 종결된 것은 빠진다
    finally:
        db.close()


def test_이벤트가_나중에_종결돼도_인계_시점_기록은_남는다(seeded_users):
    """사후 검토는 그 시점의 사실을 묻지, 지금의 사실을 묻지 않는다."""
    db = get_session()
    try:
        ev = _event(db, place="한밤중지점")
        db.commit()
        row = H.create(db, shift_name="야간", summary="주시 필요")
        H.submit(db, row.id)
        db.commit()

        ev.status = E.CLOSED
        db.commit()

        db.refresh(row)
        assert [i["place"] for i in H.open_items(row)] == ["한밤중지점"]
    finally:
        db.close()


def test_SOP_미이행_건수가_스냅샷에_실린다(seeded_users):
    """인수자가 이벤트마다 눌러 보지 않고 「무엇이 남았는지」를 알아야 한다."""
    db = get_session()
    try:
        sop.seed_builtin(db)
        _event(db)
        db.commit()
        row = H.create(db, shift_name="야간", summary="x")
        db.commit()
        item = H.open_items(row)[0]
        assert item["sop_total"] > 0
        assert item["sop_left"] == item["sop_total"]   # 아직 아무것도 안 찍음
    finally:
        db.close()


# --- 상태 흐름 ---------------------------------------------------------------

def test_빈_인계문은_넘길_수_없다(seeded_users):
    db = get_session()
    try:
        row = H.create(db, shift_name="야간", summary="   ")
        db.commit()
        with pytest.raises(ValueError, match="인계 내용"):
            H.submit(db, row.id)
    finally:
        db.rollback()
        db.close()


def test_근무조를_안_적으면_만들_수_없다(seeded_users):
    db = get_session()
    try:
        with pytest.raises(ValueError, match="근무조"):
            H.create(db, shift_name="", summary="내용")
    finally:
        db.rollback()
        db.close()


def test_넘긴_뒤에는_고칠_수_없다(seeded_users):
    db = get_session()
    try:
        row = H.create(db, shift_name="야간", summary="원래 내용")
        H.submit(db, row.id)
        db.commit()
        with pytest.raises(ValueError, match="고칠 수 없습니다"):
            H.update(db, row.id, summary="몰래 바꾼 내용")
        db.refresh(row)
        assert row.summary == "원래 내용"
    finally:
        db.rollback()
        db.close()


def test_인계자_본인은_인수_확인할_수_없다(seeded_users):
    db = get_session()
    try:
        me = _user(db, seeded_users["admin"][0])
        row = H.create(db, shift_name="야간", summary="내용", user=me)
        H.submit(db, row.id)
        db.commit()
        with pytest.raises(ValueError, match="본인"):
            H.acknowledge(db, row.id, user=me)

        other = _user(db, seeded_users["opr"][0])
        done = H.acknowledge(db, row.id, user=other, note="확인함")
        db.commit()
        assert done.status == H.ACKNOWLEDGED
        # 실제로 받은 사람으로 인수자가 채워진다 — 교대는 계획대로 안 된다.
        assert done.to_login == other.login_id
    finally:
        db.close()


def test_확인_대기_목록에서_내가_넘긴_것은_빠진다(seeded_users):
    db = get_session()
    try:
        me = _user(db, seeded_users["admin"][0])
        other = _user(db, seeded_users["opr"][0])
        mine = H.create(db, shift_name="야간", summary="내가 씀", user=me)
        theirs = H.create(db, shift_name="주간", summary="남이 씀", user=other)
        H.submit(db, mine.id)
        H.submit(db, theirs.id)
        db.commit()

        ids = {r.id for r in H.pending_ack(db, exclude_user_id=me.id)}
        assert ids == {theirs.id}
    finally:
        db.close()


# --- 화면 -------------------------------------------------------------------

def test_삭제_라우트가_아예_없다():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith("/handover") and "delete" in p for p in paths)


def test_화면이_열리고_붙을_이벤트를_미리_보여_준다(client):
    db = get_session()
    try:
        _event(db, place="미리보기지점")
        db.commit()
    finally:
        db.close()

    r = client.get("/handover")
    assert r.status_code == 200
    assert "교대 인수인계" in r.text
    # 「무엇이 넘어가는가」를 제출 전에 알아야 요약을 제대로 쓴다.
    assert "미리보기지점" in r.text
    assert "삭제할 수 없습니다" in r.text


def test_화면에서_인계하면_이벤트가_함께_굳는다(client):
    db = get_session()
    try:
        _event(db, place="인계대상지점")
        db.commit()
    finally:
        db.close()

    r = client.post("/handover/save",
                    data={"shift_name": "야간", "summary": "밤새 비가 왔습니다.",
                          "todo": "아침에 현장 확인", "submit": "1"},
                    follow_redirects=False)
    assert r.status_code == 303

    db = get_session()
    try:
        row = db.scalars(select(ShiftHandover)).one()
        assert row.status == H.SUBMITTED
        assert [i["place"] for i in H.open_items(row)] == ["인계대상지점"]
        assert "handover.submit" in db.scalars(select(AuditLog.action)).all()
    finally:
        db.close()


def test_저장은_인계가_아니고_이어서_쓸_수_있다(client):
    r = client.post("/handover/save",
                    data={"shift_name": "야간", "summary": "쓰는 중"},
                    follow_redirects=False)
    assert r.status_code == 303

    db = get_session()
    try:
        row = db.scalars(select(ShiftHandover)).one()
        assert row.status == H.DRAFT
        rid = row.id
    finally:
        db.close()

    # 같은 문서를 이어서 쓴다 — 새 문서가 생기면 어느 것이 진짜인지 모른다.
    client.post("/handover/save",
                data={"row_id": rid, "shift_name": "야간",
                      "summary": "이어서 씀"}, follow_redirects=False)
    db = get_session()
    try:
        rows = db.scalars(select(ShiftHandover)).all()
        assert len(rows) == 1 and rows[0].summary == "이어서 씀"
    finally:
        db.close()


def test_빈_인계문_제출은_400이고_적은_값이_돌아온다(client):
    r = client.post("/handover/save",
                    data={"shift_name": "야간", "summary": "",
                          "todo": "이 문장이 살아 있어야 한다", "submit": "1"})
    assert r.status_code == 400
    assert "이 문장이 살아 있어야 한다" in r.text


def test_관제요원이_작성도_인수확인도_할_수_있다(anon_client, seeded_users, login):
    """교대는 상황실 근무자끼리 하는 일이다."""
    db = get_session()
    try:
        admin = _user(db, seeded_users["admin"][0])
        row = H.create(db, shift_name="야간", summary="관리자가 넘김", user=admin)
        H.submit(db, row.id)
        db.commit()
        rid = row.id
    finally:
        db.close()

    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/handover").status_code == 200
        r = anon_client.post("/handover/ack", data={"row_id": rid},
                             follow_redirects=False)
        assert r.status_code == 303
        r2 = anon_client.post("/handover/save",
                              data={"shift_name": "주간", "summary": "관제요원 작성"},
                              follow_redirects=False)
        assert r2.status_code == 303
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])

    db = get_session()
    try:
        assert db.get(ShiftHandover, rid).status == H.ACKNOWLEDGED
    finally:
        db.close()


def test_권한표():
    for role in (R.Role.SYS, R.Role.MGR, R.Role.OPR):
        assert R.can(role, R.HANDOVER, R.Action.EDIT)
        assert R.can(role, R.HANDOVER, R.Action.APPROVE)
