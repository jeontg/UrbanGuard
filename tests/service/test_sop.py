"""디지털 SOP (S-86 정의 · S-03 이행).

지켜야 할 것.

* **등급은 정확히 일치한다** — 「경계」 이벤트에 「주의」 단계까지 붙이면
  체크리스트가 길어져 아무도 안 읽는다. 공통(전체) 단계만 함께 붙는다
* **막지 않고 경고한다** — 필수 미이행이어도 종결은 된다. 막으면 사람은 아무
  칸이나 찍고 넘어간다. 대신 미이행 사실이 조치 이력에 남는다
* **「해당 없음」은 사유가 있어야 한다** — 사유 없이 넘길 수 있으면 체크리스트가
  한 번에 다 넘어간다
* **기본안은 기관 매뉴얼이 아니다** — 화면이 「기본안 n/n」으로 그 사실을
  드러내야 한다. 한 번이라도 고치면 표시가 떨어진다
* **다시 심어도 덮어쓰지 않는다** — 고쳐 둔 내용이 배포마다 되돌아가면 아무도
  고치지 않는다
* **단계를 지워도 이행 기록은 남는다** — 「무엇을 했는지」가 사라지면 안 된다
* **관제요원도 체크할 수 있다** — SOP 를 따르는 것은 이벤트 처리 행위다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import events as E
from tot_dashboard.core import roles as R
from tot_dashboard.core import sop
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (Event, EventAction, EventSopCheck,
                                       SopStep)
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
    db = get_session()
    try:
        sop.seed_builtin(db)
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(EventSopCheck))
        s.execute(sa_delete(EventAction))
        s.execute(sa_delete(Event))
        s.execute(sa_delete(SopStep))
        s.commit()
    finally:
        s.close()


def _event(db, *, domain="flood", level="경계") -> Event:
    ev = Event(domain=domain, block_id="BLOCK-TEST", place_name="시험지점",
               event_type="침수", level=level, peak_level=level,
               status=E.OPEN)
    db.add(ev)
    db.flush()
    return ev


# --- 단계 정의 ---------------------------------------------------------------

def test_등급은_정확히_일치하고_공통_단계만_함께_붙는다():
    db = get_session()
    try:
        titles = {s.title for s in sop.steps_for(db, "flood", "경계")}
        levels = {s.level for s in sop.steps_for(db, "flood", "경계")}
        # 「주의」 단계는 섞이지 않는다.
        assert levels <= {"", "경계"}
        # 공통 단계는 도메인·등급과 무관하게 붙는다.
        assert "영상으로 현장 확인" in titles
        # 다른 도메인 단계는 붙지 않는다.
        assert all(s.domain in ("", "flood")
                   for s in sop.steps_for(db, "flood", "경계"))
    finally:
        db.close()


def test_다시_심어도_고쳐_둔_내용을_덮어쓰지_않는다():
    db = get_session()
    try:
        row = sop.all_steps(db)[0]
        sop.save_step(db, step_id=row.id, domain=row.domain, level=row.level,
                      title="기관이 고친 제목", detail="기관 내용", seq=row.seq,
                      by="tester")
        db.commit()
        added = sop.seed_builtin(db)
        db.commit()
        # 고친 제목이 없으므로 기본안 1개가 다시 들어오지만, **고친 줄은
        # 그대로 남는다** — 되돌아가면 아무도 고치지 않는다.
        assert added == 1
        assert db.get(SopStep, row.id).title == "기관이 고친 제목"
        assert db.get(SopStep, row.id).builtin is False
    finally:
        db.close()


def test_기본안_비율이_고칠수록_줄어든다():
    db = get_session()
    try:
        stock0, total0 = sop.builtin_ratio(db)
        assert stock0 == total0 > 0   # 심은 직후는 전부 기본안
        row = sop.all_steps(db)[0]
        sop.save_step(db, step_id=row.id, domain=row.domain, level=row.level,
                      title=row.title + " (기관)", by="tester")
        db.commit()
        stock1, total1 = sop.builtin_ratio(db)
        assert stock1 == stock0 - 1 and total1 == total0
    finally:
        db.close()


def test_제목이_비면_저장되지_않는다():
    db = get_session()
    try:
        with pytest.raises(ValueError, match="제목"):
            sop.save_step(db, step_id=None, domain="", level="", title="  ")
    finally:
        db.rollback()
        db.close()


# --- 이행 기록 ---------------------------------------------------------------

def test_해당_없음은_사유가_있어야_한다():
    db = get_session()
    try:
        ev = _event(db)
        step = sop.steps_for(db, ev.domain, ev.level)[0]
        with pytest.raises(ValueError, match="사유"):
            sop.check(db, ev.id, step.id, skipped=True, note="")
        # 사유가 있으면 넘어간다.
        row = sop.check(db, ev.id, step.id, skipped=True, note="야간 미운영 구간")
        assert row.skipped is True
        db.commit()
    finally:
        db.close()


def test_단계를_지워도_이행_기록은_남는다():
    db = get_session()
    try:
        ev = _event(db)
        step = sop.steps_for(db, ev.domain, ev.level)[0]
        sop.check(db, ev.id, step.id, note="확인함")
        db.commit()
        title = step.title

        sop.delete_step(db, step.id)
        db.commit()

        rows = sop.history(db, ev.id)
        assert len(rows) == 1
        assert rows[0].step_id is None      # 단계는 사라졌지만
        assert rows[0].step_title == title  # 무엇을 했는지는 남는다
    finally:
        db.close()


def test_진행률과_요약문():
    db = get_session()
    try:
        ev = _event(db)
        steps = sop.steps_for(db, ev.domain, ev.level)
        p0 = sop.progress(db, ev)
        assert p0["done"] == 0 and p0["required_left"] == p0["total"] > 0
        assert "미이행" in sop.summary_text(db, ev)

        for s in steps:
            sop.check(db, ev.id, s.id)
        db.commit()
        p1 = sop.progress(db, ev)
        assert p1["done"] == p1["total"] and p1["required_left"] == 0
        assert "미이행" not in sop.summary_text(db, ev)
    finally:
        db.close()


def test_등급이_오르면_상위_단계가_새로_뜬다():
    db = get_session()
    try:
        ev = _event(db, level="경계")
        n_warn = len(sop.progress(db, ev)["steps"])
        ev.level = "심각"
        db.flush()
        n_crit = len(sop.progress(db, ev)["steps"])
        assert n_crit > n_warn
    finally:
        db.rollback()
        db.close()


# --- 화면 -------------------------------------------------------------------

def test_편집_화면이_열리고_기본안_경고가_보인다(client):
    r = client.get("/settings/sop")
    assert r.status_code == 200
    assert "디지털 SOP" in r.text
    # 바뀌지 않았다는 사실을 화면이 스스로 말해야 한다.
    assert "기본안" in r.text
    assert "어느 기관의 행동매뉴얼도 아닙니다" in r.text


def test_체크리스트가_이벤트_상세에_나온다(client):
    db = get_session()
    try:
        ev = _event(db)
        db.commit()
        eid, title = ev.id, sop.steps_for(db, ev.domain, ev.level)[0].title
    finally:
        db.close()

    r = client.get(f"/events/{eid}")
    assert r.status_code == 200
    assert "조치 절차 (SOP)" in r.text
    assert title in r.text


def test_화면에서_체크하면_조치_이력에도_남는다(client):
    db = get_session()
    try:
        ev = _event(db)
        db.commit()
        eid = ev.id
        step = sop.steps_for(db, ev.domain, ev.level)[0]
        sid, title = step.id, step.title
    finally:
        db.close()

    r = client.post(f"/events/{eid}/sop",
                    data={"step_id": sid, "op": "check", "note": "확인 완료"})
    assert r.status_code == 200

    db = get_session()
    try:
        assert len(sop.history(db, eid)) == 1
        # 체크리스트만 보면 「언제 무엇을」이 나오지만, 이벤트 이력을 읽는
        # 사람에게도 보여야 사후 검토가 한 곳에서 끝난다.
        memos = db.scalars(select(EventAction.memo)
                           .where(EventAction.event_id == eid)).all()
        assert any(title in m for m in memos)
    finally:
        db.close()


def test_필수_미이행이어도_종결되고_사실이_기록된다(client):
    """막지 않는다. 막으면 사람은 아무 칸이나 찍고 넘어간다."""
    db = get_session()
    try:
        ev = _event(db)
        db.commit()
        eid = ev.id
    finally:
        db.close()

    r = client.post(f"/events/{eid}/action", data={"act": "close", "memo": ""})
    assert r.status_code == 200

    db = get_session()
    try:
        ev = db.get(Event, eid)
        assert ev.status == E.CLOSED
        memos = db.scalars(select(EventAction.memo)
                           .where(EventAction.event_id == eid)).all()
        # 「SOP 0/n (필수 n건 미이행)」이 종결 메모에 박혀 있어야 한다.
        assert any("미이행" in m for m in memos)
    finally:
        db.close()


def test_종결된_이벤트는_체크할_수_없다(client):
    db = get_session()
    try:
        ev = _event(db)
        ev.status = E.CLOSED
        db.commit()
        eid = ev.id
        sid = sop.steps_for(db, ev.domain, ev.level)[0].id
    finally:
        db.close()

    r = client.post(f"/events/{eid}/sop", data={"step_id": sid, "op": "check"})
    assert r.status_code == 400


def test_관제요원은_체크는_되고_단계_편집은_안_된다(anon_client, seeded_users, login):
    db = get_session()
    try:
        ev = _event(db)
        db.commit()
        eid = ev.id
        sid = sop.steps_for(db, ev.domain, ev.level)[0].id
    finally:
        db.close()

    login(anon_client, *seeded_users["opr"])
    try:
        # SOP 를 따르는 것은 이벤트 처리 행위라 관제요원도 찍을 수 있다.
        assert anon_client.post(f"/events/{eid}/sop",
                                data={"step_id": sid, "op": "check"}
                                ).status_code == 200
        # 단계 정의를 고치는 것은 별개다.
        assert anon_client.get("/settings/sop").status_code == 200
        r = anon_client.post("/settings/sop/save",
                             data={"title": "관제요원이 만든 단계"},
                             follow_redirects=False)
        assert r.status_code in (302, 303, 403)
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


def test_권한표(client):
    assert R.can(R.Role.SYS, R.SOP, R.Action.EDIT)
    assert R.can(R.Role.MGR, R.SOP, R.Action.EDIT)
    assert R.can(R.Role.OPR, R.SOP, R.Action.VIEW)
    assert not R.can(R.Role.OPR, R.SOP, R.Action.EDIT)
