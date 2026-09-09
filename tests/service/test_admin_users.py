"""사용자·권한 관리 화면의 계정 복구 조치 (S-90).

지켜야 할 것.

* **잠금 해제와 비밀번호 초기화가 구분된다** — 잠김의 대부분은 단순 오타다.
  둘을 합쳐 두면 멀쩡한 비밀번호까지 버리게 된다
* **화면과 서버 CLI 가 같은 코드를 쓴다** — 한쪽만 실패 횟수를 안 지운다든가
  하는 어긋남이 생기면 안 된다
* **시스템관리자만 할 수 있다** — 남의 계정 비밀번호를 바꾸는 일이다
* **화면에서 잠김 여부가 보인다** — 「로그인이 안 된다」는 문의를 받았을 때
  원인이 잠금인지 비밀번호인지 여기서 갈린다
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from fastapi.testclient import TestClient

from tot_dashboard.core import account_recovery as AR
from tot_dashboard.core.bootstrap import create_user
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog, User
from tot_dashboard.core.roles import Role
from tot_dashboard.core.security import verify_password
from tot_dashboard.service.main import app

LOGIN = "s90_target"
OLD_PW = "OldPass!2026"


@pytest.fixture(scope="module")
def anon_client():
    # 수명주기를 켜지 않는다 — main.py 의 runner 스레드는 프로세스당 한 번만
    # start 할 수 있어, 다른 시험 모듈이 뒤이어 열 때 전부 깨진다.
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def target(db_schema):
    _purge()
    db = get_session()
    try:
        create_user(db, login_id=LOGIN, name="화면대상", dept="상황실",
                    role=Role.OPR.value, password=OLD_PW, domains=[],
                    must_change=False)
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def _purge():
    s = get_session()
    try:
        u = s.scalar(select(User).where(User.login_id == LOGIN))
        if u is not None:
            s.execute(delete(AuditLog).where(AuditLog.user_id == u.id))
            s.delete(u)
        s.execute(delete(AuditLog).where(AuditLog.target == LOGIN))
        s.commit()
    finally:
        s.close()


def _get() -> User:
    s = get_session()
    try:
        return s.scalar(select(User).where(User.login_id == LOGIN))
    finally:
        s.close()


def _lock():
    s = get_session()
    try:
        u = s.scalar(select(User).where(User.login_id == LOGIN))
        u.failed_count = 5
        u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        s.commit()
    finally:
        s.close()


# --- 권한 -------------------------------------------------------------------
def test_관제요원은_잠금을_해제할_수_없다(anon_client, seeded_users, login):
    uid = _get().id
    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.post(f"/admin/users/{uid}/unlock").status_code == 403
    finally:
        anon_client.cookies.clear()


# --- 잠금 해제 ---------------------------------------------------------------
def test_잠금_해제는_비밀번호를_건드리지_않는다(client):
    _lock()
    res = client.post(f"/admin/users/{_get().id}/unlock")
    assert res.status_code == 200
    assert "잠금을 해제했습니다" in res.text

    u = _get()
    assert u.locked_until is None and u.failed_count == 0
    assert verify_password(OLD_PW, u.pw_hash), "잠금만 풀랬는데 비밀번호가 바뀌었다"
    assert u.must_change_password is False


def test_잠기지_않은_계정은_그렇다고_알려_준다(client):
    res = client.post(f"/admin/users/{_get().id}/unlock")
    assert "잠겨 있지 않았습니다" in res.text


def test_잠금_해제는_감사_로그에_남는다(client):
    _lock()
    client.post(f"/admin/users/{_get().id}/unlock")
    s = get_session()
    try:
        rows = s.scalars(select(AuditLog).where(
            AuditLog.action == AR.USER_UNLOCK, AuditLog.target == LOGIN)).all()
        assert rows, "잠금 해제가 감사 로그에 남지 않았다"
    finally:
        s.close()


# --- 비밀번호 초기화 ---------------------------------------------------------
def test_초기화하면_임시_비밀번호를_한_번_보여_준다(client):
    res = client.post(f"/admin/users/{_get().id}/reset")
    assert res.status_code == 200
    assert "임시 비밀번호" in res.text
    u = _get()
    assert not verify_password(OLD_PW, u.pw_hash)
    assert u.must_change_password is True


def test_잠긴_계정을_초기화하면_잠금도_함께_풀린다(client):
    """새 비밀번호를 줬는데 잠겨 있으면 여전히 못 들어온다."""
    _lock()
    res = client.post(f"/admin/users/{_get().id}/reset")
    assert "잠금도 함께 해제" in res.text
    u = _get()
    assert u.locked_until is None and u.failed_count == 0


def test_없는_계정은_404(client):
    assert client.post("/admin/users/999999/reset").status_code == 404
    assert client.post("/admin/users/999999/unlock").status_code == 404


# --- 화면 표시 ---------------------------------------------------------------
def test_잠긴_계정은_목록에서_구분된다(client):
    _lock()
    body = client.get("/admin/users").text
    assert "잠김" in body
    assert "실패 5회" in body


def test_임시_비밀번호_상태가_보인다(client):
    client.post(f"/admin/users/{_get().id}/reset")
    assert "임시 비밀번호" in client.get("/admin/users").text
