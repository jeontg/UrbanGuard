"""알림 요청·승인 (S-50 / S-51).

핵심은 **되돌릴 수 없는 주민 경보가 2인 승인을 거치는가**다.
설계서 8절 「알림 3계층」.
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import notifications as N
from tot_dashboard.core import roles as R


@pytest.fixture
def db(db_schema):
    """notifications.requested_by 는 users 를 참조하므로 실제 계정이 필요하다."""
    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import Notification, User
    s = get_session()
    s.query(Notification).delete()
    s.query(User).filter(User.login_id.like("nt_%")).delete(synchronize_session=False)
    s.commit()
    for lid, role in (("nt_opr", R.Role.OPR), ("nt_mgr", R.Role.MGR),
                      ("nt_sys", R.Role.SYS)):
        create_user(s, login_id=lid, name=lid, dept="테스트", role=role.value,
                    password="NotifyTest!2026", domains=[d.value for d in R.Domain],
                    must_change=False)
    s.commit()
    yield s
    s.query(Notification).delete()
    s.query(User).filter(User.login_id.like("nt_%")).delete(synchronize_session=False)
    s.commit()
    s.close()


def _u(db, login_id):
    from tot_dashboard.core.models import User
    return db.query(User).filter(User.login_id == login_id).one()


@pytest.fixture
def opr(db):
    return _u(db, "nt_opr")


@pytest.fixture
def mgr(db):
    return _u(db, "nt_mgr")


@pytest.fixture
def sys_user(db):
    return _u(db, "nt_sys")


def _req(db, tier, user, level="주의"):
    n = N.request(db, event_id=1, domain="flood", tier=tier,
                  body="테스트 문안", user=user, risk_level=level)
    db.commit()
    return n


# --- 계층 구분 ---------------------------------------------------------------
def test_dept_and_resident_are_distinguishable(db, opr):
    d = _req(db, N.TIER_DEPT, opr)
    r = _req(db, N.TIER_RESIDENT, opr)
    assert N.tier_of(d) == N.TIER_DEPT
    assert N.tier_of(r) == N.TIER_RESIDENT


# --- 자가 승인 가능 여부 ------------------------------------------------------
def test_dept_notice_can_be_sent_by_operator_alone(opr):
    """부서 통보는 되돌릴 수 있으므로 관제요원 단독으로 진행한다."""
    assert N.can_self_approve(opr, N.TIER_DEPT) is True


def test_resident_alert_needs_second_person_by_default(opr, monkeypatch):
    """주민 경보 기본값은 2인 승인 — 되돌릴 수 없는 행위이기 때문이다."""
    monkeypatch.delenv("URBANGUARD_SOLO_SEND_ON_CRITICAL", raising=False)
    assert N.can_self_approve(opr, N.TIER_RESIDENT, "심각") is False
    assert N.can_self_approve(opr, N.TIER_RESIDENT, "경계") is False


def test_solo_send_opens_only_for_critical_when_enabled(opr, monkeypatch):
    monkeypatch.setenv("URBANGUARD_SOLO_SEND_ON_CRITICAL", "1")
    assert N.can_self_approve(opr, N.TIER_RESIDENT, "심각") is True
    assert N.can_self_approve(opr, N.TIER_RESIDENT, "경계") is False


def test_resident_alert_blocks_self_approval_for_every_role(mgr, sys_user,
                                                            monkeypatch):
    """주민 경보는 역할과 무관하게 2인 승인이다.

    부서담당자·시스템관리자가 자기 요청을 스스로 승인할 수 있으면
    「2인 승인」이 1인이 되어 제도가 무력해진다.
    """
    monkeypatch.delenv("URBANGUARD_SOLO_SEND_ON_CRITICAL", raising=False)
    assert N.can_self_approve(mgr, N.TIER_RESIDENT, "심각") is False
    assert N.can_self_approve(sys_user, N.TIER_RESIDENT, "심각") is False


def test_dept_notice_is_single_person_for_every_role(mgr, sys_user):
    """부서 통보는 되돌릴 수 있으므로 단독 진행한다."""
    assert N.can_self_approve(mgr, N.TIER_DEPT) is True
    assert N.can_self_approve(sys_user, N.TIER_DEPT) is True


# --- 상태 전이 ---------------------------------------------------------------
def test_request_starts_as_pending(db, opr):
    n = _req(db, N.TIER_RESIDENT, opr)
    assert n.status == N.REQUESTED
    assert N.pending_count(db) == 1


def test_approve_sets_approver_and_time(db, opr, mgr):
    n = _req(db, N.TIER_RESIDENT, opr)
    N.approve(db, n, mgr)
    db.commit()
    assert n.status == N.APPROVED
    assert n.approved_by == mgr.id and n.approved_at is not None
    assert N.pending_count(db) == 0


def test_post_approval_flag_marks_solo_send(db, opr):
    """「심각」 단독 발송은 사후 승인 대상으로 표시돼야 추적이 된다."""
    n = _req(db, N.TIER_RESIDENT, opr, level="심각")
    N.approve(db, n, opr, post_approval=True)
    db.commit()
    assert n.needs_post_approval is True


def test_reject_records_reason(db, opr, mgr):
    n = _req(db, N.TIER_DEPT, opr)
    N.reject(db, n, mgr, "문안 수정 필요")
    db.commit()
    assert n.status == N.REJECTED and "문안" in n.reject_reason


def test_mark_sent_and_failed(db, opr):
    a = _req(db, N.TIER_DEPT, opr)
    N.mark_sent(db, a, ok=True)
    b = _req(db, N.TIER_DEPT, opr)
    N.mark_sent(db, b, ok=False, reason="연동 없음")
    db.commit()
    assert a.status == N.SENT and a.sent_at is not None
    assert b.status == N.FAILED and "연동" in b.reject_reason


# --- 도메인 범위 -------------------------------------------------------------
def test_domain_scope_filters_pending(db, opr):
    N.request(db, event_id=1, domain="flood", tier=N.TIER_DEPT,
              body="f", user=opr)
    N.request(db, event_id=2, domain="road", tier=N.TIER_DEPT,
              body="r", user=opr)
    db.commit()
    assert {n.domain for n in N.list_pending(db, {"road"})} == {"road"}
    assert N.pending_count(db, set()) == 0


def test_history_filters_by_status(db, opr):
    a = _req(db, N.TIER_DEPT, opr)
    N.mark_sent(db, a, ok=True)
    _req(db, N.TIER_DEPT, opr)
    db.commit()
    assert len(N.list_history(db, status=N.SENT)) == 1
    assert len(N.list_history(db)) == 2
