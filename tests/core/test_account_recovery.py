"""계정 복구 — 비밀번호 초기화·잠금 해제 (core/account_recovery.py).

지켜야 할 것.

* **아무도 로그인할 수 없어도 복구할 수 있다** — 시스템관리자 비밀번호를 잃으면
  화면으로는 아무것도 못 한다. 서버 CLI 가 유일한 출구다
* **초기화하면 잠금도 함께 풀린다** — 새 비밀번호를 줬는데 잠겨 있으면 여전히
  못 들어온다. 두 번 조작하게 만들 이유가 없다
* **잠금 해제는 비밀번호를 건드리지 않는다** — 잠김의 대부분은 단순 오타다.
  멀쩡한 비밀번호를 버리면 안 된다
* **초기화한 비밀번호는 반드시 바꿔야 쓸 수 있다** — 초기화한 사람이 아는
  비밀번호가 계속 살아 있으면 안 된다
* **복구 행위는 감사 로그에 남는다** — 서버에서 직접 푼 것 자체가 감사 대상이다
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from tot_dashboard.core import account_recovery as AR
from tot_dashboard.core import audit
from tot_dashboard.core.bootstrap import create_user
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog, User
from tot_dashboard.core.roles import Role
from tot_dashboard.core.security import verify_password

pytestmark = pytest.mark.usefixtures("db_schema")

LOGIN = "recovery_target"
OLD_PW = "OldPass!2026"


@pytest.fixture()
def db():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def target(db):
    _purge()
    create_user(db, login_id=LOGIN, name="복구대상", dept="정보통신과",
                role=Role.SYS.value, password=OLD_PW, domains=[],
                must_change=False)
    db.commit()
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


def _reload(db) -> User:
    db.expire_all()
    return db.scalar(select(User).where(User.login_id == LOGIN))


def _lock(db, *, failed=5, minutes=10):
    u = _reload(db)
    u.failed_count = failed
    u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.commit()


# --- 초기화 -----------------------------------------------------------------
def test_초기화하면_새_비밀번호로_바뀐다(db):
    result = AR.reset_password(db, LOGIN)
    u = _reload(db)
    assert verify_password(result.password, u.pw_hash)
    assert not verify_password(OLD_PW, u.pw_hash), "예전 비밀번호가 아직 먹힌다"


def test_초기화한_비밀번호는_바꿔야_쓸_수_있다(db):
    """초기화한 사람이 아는 비밀번호가 계속 살아 있으면 안 된다."""
    AR.reset_password(db, LOGIN)
    assert _reload(db).must_change_password is True


def test_초기화하면_잠금도_함께_풀린다(db):
    _lock(db)
    result = AR.reset_password(db, LOGIN)
    u = _reload(db)
    assert u.locked_until is None
    assert u.failed_count == 0
    # 잠겨 있었다는 사실은 호출자에게 알려 줘야 화면·CLI 가 안내할 수 있다.
    assert result.was_locked is True
    assert result.failed_count == 5


def test_비밀번호를_직접_지정할_수_있다(db):
    AR.reset_password(db, LOGIN, password="Chosen!Pass2026")
    assert verify_password("Chosen!Pass2026", _reload(db).pw_hash)


def test_정책에_안_맞는_비밀번호는_거부한다(db):
    with pytest.raises(ValueError):
        AR.reset_password(db, LOGIN, password="short")
    assert verify_password(OLD_PW, _reload(db).pw_hash), "거부했는데 바뀌었다"


def test_없는_계정은_분명한_오류를_낸다(db):
    with pytest.raises(LookupError):
        AR.reset_password(db, "no_such_account")


def test_비활성_계정은_초기화하지_않는다(db):
    """비밀번호만 줘 봐야 로그인되지 않는다. 먼저 활성화해야 한다."""
    u = _reload(db)
    u.is_active = False
    db.commit()
    with pytest.raises(ValueError, match="비활성"):
        AR.reset_password(db, LOGIN)


def test_초기화는_감사_로그에_남는다(db):
    AR.reset_password(db, LOGIN)
    rows = db.scalars(
        select(AuditLog).where(AuditLog.action == audit.USER_PASSWORD_RESET,
                               AuditLog.target == LOGIN)).all()
    assert rows, "초기화가 감사 로그에 남지 않았다"


def test_서버_CLI_로_한_것이_구분된다(db):
    """서버에 직접 접근해 푼 것 자체가 감사 대상이다."""
    AR.reset_password(db, LOGIN)
    row = db.scalars(
        select(AuditLog).where(AuditLog.target == LOGIN)).all()[-1]
    assert row.login_id == AR.CLI_ACTOR
    assert row.ip == AR.CLI_IP


def test_화면에서_한_것은_실행자로_남는다(db):
    actor = db.scalar(select(User).where(User.login_id == LOGIN))
    AR.reset_password(db, LOGIN, actor_user=actor, ip="10.0.0.5")
    row = db.scalars(
        select(AuditLog).where(AuditLog.target == LOGIN)).all()[-1]
    assert row.login_id == LOGIN
    assert row.ip == "10.0.0.5"


# --- 잠금 해제 ---------------------------------------------------------------
def test_잠금_해제는_비밀번호를_건드리지_않는다(db):
    _lock(db)
    assert AR.unlock(db, LOGIN) is True
    u = _reload(db)
    assert u.locked_until is None and u.failed_count == 0
    assert verify_password(OLD_PW, u.pw_hash), "잠금만 풀랬는데 비밀번호가 바뀌었다"
    assert u.must_change_password is False


def test_잠기지_않은_계정은_변경_없음을_알린다(db):
    assert AR.unlock(db, LOGIN) is False


def test_실패_횟수만_쌓인_계정도_풀린다(db):
    """잠기기 직전 상태에서 미리 지워 두는 것도 정상 조치다."""
    _lock(db, failed=3)
    u = _reload(db)
    u.locked_until = None
    db.commit()
    assert AR.unlock(db, LOGIN) is True
    assert _reload(db).failed_count == 0


# --- 조회 -------------------------------------------------------------------
def test_시스템관리자_계정을_찾아_준다(db):
    ids = [u.login_id for u in AR.admin_accounts(db)]
    assert LOGIN in ids


def test_관제요원은_관리자_목록에_없다(db):
    create_user(db, login_id="recovery_opr", name="관제", dept="상황실",
                role=Role.OPR.value, password="OprPass!2026", domains=[],
                must_change=False)
    db.commit()
    try:
        assert "recovery_opr" not in [u.login_id for u in AR.admin_accounts(db)]
    finally:
        u = db.scalar(select(User).where(User.login_id == "recovery_opr"))
        db.delete(u)
        db.commit()


# --- CLI --------------------------------------------------------------------
def test_CLI_는_확인_없이는_바꾸지_않는다(db, capsys):
    """스크립트·CI 에서 실수로 돌아도 조용히 초기화되면 안 된다."""
    assert AR.main(["--login-id", LOGIN]) == 1
    assert verify_password(OLD_PW, _reload(db).pw_hash)
    assert "--yes" in capsys.readouterr().err


def test_CLI_초기화(db, capsys):
    assert AR.main(["--login-id", LOGIN, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "임시 비밀번호" in out
    assert _reload(db).must_change_password is True


def test_CLI_잠금_해제(db, capsys):
    _lock(db)
    assert AR.main(["--login-id", LOGIN, "--unlock", "--yes"]) == 0
    u = _reload(db)
    assert u.locked_until is None
    assert verify_password(OLD_PW, u.pw_hash), "잠금만 풀랬는데 비밀번호가 바뀌었다"


def test_CLI_목록은_아무것도_바꾸지_않는다(db, capsys):
    assert AR.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert LOGIN in out
    assert verify_password(OLD_PW, _reload(db).pw_hash)


def test_CLI_는_비밀번호를_인자로_받지_않는다():
    """명령 이력(PowerShell·bash history)에 평문으로 남기 때문이다."""
    with pytest.raises(SystemExit):
        AR.main(["--login-id", LOGIN, "--password", "Secret!2026", "--yes"])


def test_CLI_없는_계정은_1을_돌려준다(db, capsys):
    assert AR.main(["--login-id", "no_such_account", "--yes"]) == 1
    assert "--list" in capsys.readouterr().err, "다음에 뭘 할지 알려 줘야 한다"


def test_CLI_는_대상_없이는_실행되지_않는다():
    with pytest.raises(SystemExit):
        AR.main([])
