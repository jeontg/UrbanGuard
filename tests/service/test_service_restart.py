"""서비스 재기동 (S-01 우측 카드).

지켜야 할 것.

* **되살릴 사람이 없으면 막는다** — 감시 기동이 아닐 때 재기동을 허용하면
  그건 재기동이 아니라 그냥 종료다. 2026-08-12 사고가 그 모습이었다
* **시스템관리자만** — 관제요원이 실수로 누르면 상시 탐지가 전부 멎는다
* **기록이 먼저** — 죽은 뒤에는 아무것도 남길 수 없으므로 감사 로그를 먼저
  커밋하고 죽는다
* **두 번 눌러도 한 번만** — 예약이 겹치면 정리 도중에 또 죽는다
* **종료 코드는 42** — 0이면 감시 프로세스가 「정상 종료」로 보고 안 되살린다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import roles as R
from tot_dashboard.core import service_control as SC
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog
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
    # 시험이 진짜로 프로세스를 죽이면 안 된다. 예약 표시를 매번 되돌린다.
    SC._pending.clear()
    yield
    SC._pending.clear()
    _purge()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(AuditLog))
        s.commit()
    finally:
        s.close()


@pytest.fixture
def supervised(monkeypatch):
    """감시 기동 중인 것처럼 꾸민다."""
    monkeypatch.setenv(SC.SUPERVISED_ENV, "1")
    return True


@pytest.fixture
def no_kill(monkeypatch):
    """**실제로 죽지 않게 막는다.**

    이 시험은 「재기동을 예약했는가」까지만 확인한다. 진짜로 ``os._exit`` 가
    돌면 pytest 프로세스가 통째로 사라져 시험 결과조차 안 남는다.
    """
    calls = []
    monkeypatch.setattr(SC, "request_restart",
                        lambda shutdown, **kw: (calls.append(shutdown), True)[1])
    return calls


# --- 코어 -------------------------------------------------------------------

def test_감시_기동이_아니면_막힌다(monkeypatch):
    monkeypatch.delenv(SC.SUPERVISED_ENV, raising=False)
    ok, why = SC.can_restart()
    assert ok is False
    # 왜 막혔는지 화면에 그대로 띄우므로, 안내가 비어 있으면 안 된다.
    assert "되살릴 것이 없습니다" in why


def test_감시_기동이면_가능하다(supervised):
    ok, why = SC.can_restart()
    assert ok is True and why == ""


def test_종료_코드는_0이_아니다():
    """0이면 serve.py 가 「정상 종료」로 보고 되살리지 않는다."""
    assert SC.RESTART_EXIT_CODE != 0
    assert SC.RESTART_EXIT_CODE == 42


def test_serve_py_와_상수가_같다():
    """serve.py 는 패키지 없이 단독 실행돼야 해서 상수를 복제해 뒀다.
    한쪽만 고치면 재기동이 조용히 깨지므로 시험으로 묶어 둔다."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "serve.py"
    text = src.read_text(encoding="utf-8")
    assert f"RESTART_EXIT_CODE = {SC.RESTART_EXIT_CODE}" in text
    assert f'SUPERVISED_ENV = "{SC.SUPERVISED_ENV}"' in text


def test_두_번_예약되지_않는다(supervised, monkeypatch):
    monkeypatch.setattr(SC.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: None})())
    assert SC.request_restart(lambda: None) is True
    assert SC.request_restart(lambda: None) is False
    ok, why = SC.can_restart()
    assert ok is False and "이미 재기동이 예약" in why


# --- 화면·API ---------------------------------------------------------------

def test_상황판에_카드가_보인다(client, supervised):
    r = client.get("/")
    assert r.status_code == 200
    assert "서비스 상태" in r.text
    assert "서비스 재기동" in r.text


def test_감시_기동이_아니면_버튼이_잠기고_이유가_보인다(client, monkeypatch):
    monkeypatch.delenv(SC.SUPERVISED_ENV, raising=False)
    r = client.get("/")
    assert r.status_code == 200
    assert "disabled" in r.text
    assert "되살릴 것이 없습니다" in r.text


def test_감시_기동이_아니면_API가_409(client, monkeypatch):
    monkeypatch.delenv(SC.SUPERVISED_ENV, raising=False)
    r = client.post("/api/service/restart")
    assert r.status_code == 409
    assert r.json()["ok"] is False

    db = get_session()
    try:
        # 막혔으면 감사 로그도 남지 않아야 한다 — 하지도 않은 일을 기록하면 안 된다.
        assert db.scalars(select(AuditLog.action)).all() == []
    finally:
        db.close()


def test_재기동하면_감사_로그가_먼저_남는다(client, supervised, no_kill):
    r = client.post("/api/service/restart")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "상시 탐지" in body["message"]
    # 예약이 실제로 걸렸는지 (no_kill 이 가로챈 호출)
    assert len(no_kill) == 1

    db = get_session()
    try:
        rows = db.scalars(select(AuditLog)).all()
        assert len(rows) == 1
        assert rows[0].action == "service.restart"
        assert "watchers_stopped" in (rows[0].after or {})
    finally:
        db.close()


def test_관제요원은_재기동할_수_없다(anon_client, seeded_users, login, supervised,
                             no_kill):
    login(anon_client, *seeded_users["opr"])
    try:
        r = anon_client.post("/api/service/restart")
        assert r.status_code == 403
        # 상황판은 보이되 카드는 없어야 한다.
        page = anon_client.get("/")
        assert page.status_code == 200
        assert "서비스 재기동" not in page.text
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])

    assert no_kill == []


def test_로그인_없이는_상태도_못_본다(anon_client):
    c = TestClient(app)
    assert c.get("/api/service/state").status_code in (401, 403)
    assert c.post("/api/service/restart").status_code in (401, 403)


def test_상태_API_는_시스템관리자_전용(client, supervised):
    s = client.get("/api/service/state").json()
    assert s["supervised"] is True
    assert "uptime_sec" in s and "watchers" in s


def test_권한은_시스템관리자만():
    """SETTINGS_SYS × EXECUTE — 새 자원을 만들지 않고 이 조합으로 잠갔다."""
    assert R.can(R.Role.SYS, R.SETTINGS_SYS, R.Action.EXECUTE)
    assert not R.can(R.Role.MGR, R.SETTINGS_SYS, R.Action.EXECUTE)
    assert not R.can(R.Role.OPR, R.SETTINGS_SYS, R.Action.EXECUTE)
