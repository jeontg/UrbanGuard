"""권한 매트릭스 검증 — docs/ui_design_spec.md v4 6-2절.

설계서의 표가 코드와 어긋나면 여기서 잡힌다. 특히 도메인 범위 검사는
개발 중 실제로 한 번 틀렸던 부분이라(관제요원이 침수 화면에서 403) 회귀
방지를 위해 명시적으로 검사한다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import roles as R

ALL_DOMAINS = {d.value for d in R.Domain}


# --- 관제요원(OPR) -----------------------------------------------------------
@pytest.mark.parametrize("domain", list(R.Domain))
def test_opr_can_monitor_every_domain_without_assignment(domain):
    """관제요원은 담당 도메인 배정이 없어도 전 도메인을 본다.

    상황실 근무자는 도메인별로 나뉘지 않고 한 사람이 셋을 동시에 본다
    (설계서 3절 「이벤트 중심」). 여기서 막히면 상황실 근무 자체가 불가능하다.
    """
    assert R.can(R.Role.OPR, R.MONITOR, R.Action.VIEW,
                 domain=domain, user_domains=set())


def test_opr_cannot_edit_monitor():
    assert not R.can(R.Role.OPR, R.MONITOR, R.Action.EDIT, domain=R.Domain.FLOOD)


def test_opr_can_request_but_not_approve_notification():
    assert R.can(R.Role.OPR, R.NOTIFY, R.Action.REQUEST, domain=R.Domain.FLOOD)
    assert not R.can(R.Role.OPR, R.NOTIFY, R.Action.APPROVE, domain=R.Domain.FLOOD)


def test_opr_has_no_admin_access():
    for res in (R.USERS, R.AUDIT, R.SETTINGS_OPS, R.SETTINGS_SYS):
        assert not R.can(R.Role.OPR, res, R.Action.VIEW), res


def test_opr_can_execute_detection():
    """AI 탐지 실행은 관제요원도 가능하다(설계서 6-2절)."""
    assert R.can(R.Role.OPR, R.DETECT, R.Action.EXECUTE, domain=R.Domain.ROAD)


# --- 부서담당자(MGR) — 유일하게 도메인 범위 검사를 받는다 ---------------------
def test_mgr_limited_to_assigned_domains():
    assigned = {R.Domain.FLOOD.value}
    assert R.can(R.Role.MGR, R.MONITOR, R.Action.EDIT,
                 domain=R.Domain.FLOOD, user_domains=assigned)
    assert not R.can(R.Role.MGR, R.MONITOR, R.Action.EDIT,
                     domain=R.Domain.CROWD, user_domains=assigned)


def test_mgr_can_approve_notification():
    assert R.can(R.Role.MGR, R.NOTIFY, R.Action.APPROVE,
                 domain=R.Domain.FLOOD, user_domains=ALL_DOMAINS)


def test_mgr_cannot_manage_users_or_system_settings():
    assert not R.can(R.Role.MGR, R.USERS, R.Action.VIEW)
    assert not R.can(R.Role.MGR, R.SETTINGS_SYS, R.Action.VIEW)


# --- 시스템관리자(SYS) -------------------------------------------------------
@pytest.mark.parametrize("domain", list(R.Domain))
def test_sys_ignores_domain_assignment(domain):
    assert R.can(R.Role.SYS, R.SETTINGS_OPS, R.Action.EDIT,
                 domain=domain, user_domains=set())


def test_sys_has_every_resource():
    for res in (R.DASHBOARD, R.MONITOR, R.REPORT, R.DETECT, R.CROWD_SOURCE,
                R.NOTIFY, R.NOTIFY_HISTORY, R.SETTINGS_OPS, R.SETTINGS_SYS,
                R.USERS, R.AUDIT):
        assert R.can(R.Role.SYS, res, R.Action.VIEW), res


# --- 주민 알림 발송 정책 -----------------------------------------------------
def test_solo_send_default_is_two_person_approval(monkeypatch):
    """기본값은 2인 승인이다 — 되돌릴 수 없는 행위를 기본으로 열어 두지 않는다."""
    monkeypatch.delenv("URBANGUARD_SOLO_SEND_ON_CRITICAL", raising=False)
    assert not R.can_send_without_approval(R.Role.OPR, "심각")
    assert not R.can_send_without_approval(R.Role.OPR, "경계")


def test_solo_send_only_on_critical_when_enabled(monkeypatch):
    monkeypatch.setenv("URBANGUARD_SOLO_SEND_ON_CRITICAL", "1")
    assert R.can_send_without_approval(R.Role.OPR, "심각")
    assert not R.can_send_without_approval(R.Role.OPR, "경계")


def test_mgr_and_sys_always_send_without_approval(monkeypatch):
    monkeypatch.delenv("URBANGUARD_SOLO_SEND_ON_CRITICAL", raising=False)
    assert R.can_send_without_approval(R.Role.MGR, "주의")
    assert R.can_send_without_approval(R.Role.SYS, "주의")


# --- 매트릭스 자체의 일관성 ---------------------------------------------------
def test_every_role_defines_every_resource():
    """역할별 권한표에 자원이 빠지면 조용히 거부된다. 누락을 명시적으로 잡는다."""
    resources = set(R.PERMISSIONS[R.Role.SYS])
    for role in R.Role:
        missing = resources - set(R.PERMISSIONS[role])
        assert not missing, f"{role.value} 권한표에 누락: {missing}"
