"""영상 반출 관리대장 (S-94).

지켜야 할 것.

* **지울 길이 없다** — 삭제 라우트를 만들지 않았고, 화면에도 삭제 버튼이 없다.
  대장은 감사 자료라 지우는 순간 쓸모가 없어진다
* **근거 없이는 등록되지 않는다** — 근거를 못 적는 반출은 하면 안 되는 반출이다
* **정정은 새 줄** — 원본은 남고 정정본이 원본을 가리킨다
* **파기 지연이 보인다** — 종이 대장에서 가장 자주 비는 칸이라 요약 맨 앞에 뒀다
* **관제요원은 열람만** — 실제로 내주는 결정은 부서·기관이 한다
* **대장을 만진 사실은 감사 로그에 남는다** — 대장의 「등록자」가 진짜 등록한
  사람인지는 대장 자신이 답하지 못한다
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import roles as R
from tot_dashboard.core import video_disclosure as vd
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog, VideoDisclosure
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    # 기동 훅을 켜지 않는 이유는 test_error_admin.py 와 같다 — 모듈 수준
    # runner 스레드는 프로세스당 한 번만 start 할 수 있다.
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
        s.execute(sa_delete(VideoDisclosure))
        s.execute(sa_delete(AuditLog))
        s.commit()
    finally:
        s.close()


def _form(**over) -> dict:
    base = {"requester_org": "○○경찰서 수사과",
            "requester_name": "경위 담당",
            "legal_basis": "수사기관 공문 (형사소송법)",
            "purpose": "교통사고 피의차량 확인",
            "camera_ids": "BLOCK-CHORYANG",
            "method": "copy", "masked": "1"}
    base.update(over)
    return base


# --- 코어 -------------------------------------------------------------------

def test_근거_없으면_등록되지_않는다():
    db = get_session()
    try:
        with pytest.raises(ValueError, match="근거"):
            vd.create(db, requester_org="○○서", legal_basis="   ")
    finally:
        db.rollback()
        db.close()


def test_요청기관_없으면_등록되지_않는다():
    db = get_session()
    try:
        with pytest.raises(ValueError, match="요청 기관"):
            vd.create(db, requester_org="", legal_basis="영장")
    finally:
        db.rollback()
        db.close()


def test_사본_제공은_파기예정일이_자동으로_잡힌다():
    """열람만 한 건은 남는 사본이 없으므로 파기 대상이 아니다."""
    db = get_session()
    try:
        copy = vd.create(db, requester_org="○○서", legal_basis="영장",
                         method="copy")
        view = vd.create(db, requester_org="○○서", legal_basis="영장",
                         method="view")
        assert copy.disposal_due is not None
        assert view.disposal_due is None
        db.commit()
    finally:
        db.close()


def test_정정은_원본을_남기고_새_줄을_만든다():
    db = get_session()
    try:
        origin = vd.create(db, requester_org="틀린기관", legal_basis="영장")
        db.commit()
        fixed = vd.correct(db, origin.id, reason="기관명을 잘못 적음",
                           requester_org="맞는기관")
        db.commit()

        assert fixed.id != origin.id
        assert fixed.corrects_id == origin.id
        assert fixed.requester_org == "맞는기관"
        # 원본은 그대로 남는다 — 고치지 않는다.
        db.refresh(origin)
        assert origin.requester_org == "틀린기관"
        # 두 번째 정정은 최신 정정본을 고치라고 막는다.
        with pytest.raises(ValueError, match="이미 정정"):
            vd.correct(db, origin.id, reason="또 고침")
    finally:
        db.rollback()
        db.close()


def test_정정_사유가_비면_거부한다():
    db = get_session()
    try:
        origin = vd.create(db, requester_org="○○서", legal_basis="영장")
        db.commit()
        with pytest.raises(ValueError, match="정정 사유"):
            vd.correct(db, origin.id, reason="  ")
    finally:
        db.rollback()
        db.close()


def test_파기_지연을_센다():
    db = get_session()
    try:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        late = vd.create(db, requester_org="○○서", legal_basis="영장",
                         method="copy", disposal_due=past)
        vd.create(db, requester_org="○○서", legal_basis="영장", method="view")
        db.commit()

        assert vd.is_overdue(late) is True
        assert vd.summary(db)["overdue"] == 1
        assert [r.id for r in vd.search(db, overdue="1")] == [late.id]

        vd.dispose(db, late.id)
        db.commit()
        assert vd.summary(db)["overdue"] == 0
    finally:
        db.close()


# --- 화면 -------------------------------------------------------------------

def test_화면이_열린다(client):
    r = client.get("/admin/disclosure")
    assert r.status_code == 200
    assert "영상 반출 관리대장" in r.text
    # 삭제할 수 없다는 사실이 화면에 적혀 있어야 한다.
    assert "삭제할 수 없습니다" in r.text


def test_삭제_라우트가_아예_없다():
    """대장을 지우는 길은 만들지 않는다. 라우트가 없다는 것을 시험으로 못박는다."""
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith("/admin/disclosure") and "delete" in p
                   for p in paths)


def test_등록하면_대장과_감사로그_양쪽에_남는다(client):
    r = client.post("/admin/disclosure/create", data=_form(),
                    follow_redirects=False)
    assert r.status_code == 303

    db = get_session()
    try:
        rows = db.scalars(select(VideoDisclosure)).all()
        assert len(rows) == 1
        assert rows[0].requester_org == "○○경찰서 수사과"
        assert rows[0].method == "copy"
        # 대장의 「등록자」가 진짜 등록한 사람인지는 감사 로그가 답한다.
        acts = db.scalars(select(AuditLog.action)).all()
        assert "disclosure.create" in acts
    finally:
        db.close()


def test_근거가_비면_400이고_적은_값이_돌아온다(client):
    r = client.post("/admin/disclosure/create",
                    data=_form(legal_basis="", purpose="이 문장이 살아 있어야 한다"))
    assert r.status_code == 400
    assert "근거" in r.text
    # 칸이 많은 폼이라 다시 채우게 하면 「나중에 적자」가 된다.
    assert "이 문장이 살아 있어야 한다" in r.text


def test_화면에서_정정하면_두_줄이_된다(client):
    client.post("/admin/disclosure/create", data=_form(),
                follow_redirects=False)
    db = get_session()
    try:
        origin_id = db.scalars(select(VideoDisclosure.id)).one()
    finally:
        db.close()

    r = client.post("/admin/disclosure/correct",
                    data=_form(origin_id=origin_id,
                               correction_reason="기관명 오기",
                               requester_org="△△경찰서 형사과"),
                    follow_redirects=False)
    assert r.status_code == 303

    db = get_session()
    try:
        rows = db.scalars(
            select(VideoDisclosure).order_by(VideoDisclosure.id)).all()
        assert len(rows) == 2
        assert rows[1].corrects_id == rows[0].id
        assert rows[1].correction_reason == "기관명 오기"
        assert "disclosure.correct" in db.scalars(select(AuditLog.action)).all()
    finally:
        db.close()


def test_관제요원은_열람만_된다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/admin/disclosure").status_code == 200
        # 등록은 막힌다 — 실제로 내주는 결정은 부서·기관이 한다.
        r = anon_client.post("/admin/disclosure/create", data=_form(),
                             follow_redirects=False)
        assert r.status_code in (302, 303, 403)
        db = get_session()
        try:
            assert db.scalars(select(VideoDisclosure)).all() == []
        finally:
            db.close()
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


def test_권한표에_세_역할이_모두_들어_있다():
    """MGR 은 등록까지, OPR 은 열람만. 표가 흔들리면 화면도 흔들린다."""
    assert R.can(R.Role.SYS, R.VIDEO_DISCLOSURE, R.Action.EDIT)
    assert R.can(R.Role.MGR, R.VIDEO_DISCLOSURE, R.Action.EDIT)
    assert R.can(R.Role.OPR, R.VIDEO_DISCLOSURE, R.Action.VIEW)
    assert not R.can(R.Role.OPR, R.VIDEO_DISCLOSURE, R.Action.EDIT)
