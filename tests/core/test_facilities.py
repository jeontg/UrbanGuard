"""시설물 제어 (S-11).

핵심은 **모드가 실제로 명령을 막는가**다. 화면에서 버튼만 숨기면 우회된다.
그리고 **연동되지 않은 시설에 보낸 명령이 성공으로 기록되지 않는가** —
성공한 줄 알고 방치하는 것이 가장 위험하다(설계서 4절).
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import facilities as F
from tot_dashboard.core import roles as R

LINKED = {"id": "F-OK", "name": "연동된 시설", "type": "gate", "link": "ok"}
UNLINKED = {"id": "F-NONE", "name": "미연계 시설", "type": "gate", "link": "none"}


@pytest.fixture
def db(db_schema):
    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import FacilityControl, User
    s = get_session()
    s.query(FacilityControl).delete()
    s.query(User).filter(User.login_id.like("fc_%")).delete(synchronize_session=False)
    s.commit()
    create_user(s, login_id="fc_sys", name="fc_sys", dept="테스트",
                role=R.Role.SYS.value, password="FacTest!2026",
                domains=[d.value for d in R.Domain], must_change=False)
    s.commit()
    yield s
    s.query(FacilityControl).delete()
    s.query(User).filter(User.login_id.like("fc_%")).delete(synchronize_session=False)
    s.commit()
    s.close()


@pytest.fixture
def user(db):
    from tot_dashboard.core.models import User
    return db.query(User).filter(User.login_id == "fc_sys").one()


# --- 모드가 명령을 막는가 -----------------------------------------------------
def test_advise_mode_forbids_remote_control():
    """「권고만」에서 원격 제어가 나가면 모드 설정이 의미가 없다."""
    allowed = F.allowed_commands(F.MODE_ADVISE)
    assert F.CMD_REMOTE not in allowed
    assert {F.CMD_STATUS, F.CMD_ADVISE} <= allowed


@pytest.mark.parametrize("mode", [F.MODE_REQUEST, F.MODE_AUTO])
def test_higher_modes_allow_remote_control(mode):
    assert F.CMD_REMOTE in F.allowed_commands(mode)


def test_blocked_command_is_recorded_not_silently_dropped(db, user):
    """막힌 명령도 기록에 남아야 한다 — 시도 자체가 감사 대상이다."""
    row = F.execute(db, facility=LINKED, command=F.CMD_REMOTE, user=user,
                    mode=F.MODE_ADVISE)
    db.commit()
    assert row.result == F.RESULT_BLOCKED
    assert len(F.history(db)) == 1


# --- 미연계 시설 -------------------------------------------------------------
def test_unlinked_facility_never_reports_success(db, user):
    """연동 규격이 없는데 성공으로 기록되면 담당자가 방치하게 된다."""
    row = F.execute(db, facility=UNLINKED, command=F.CMD_ADVISE, user=user,
                    mode=F.MODE_ADVISE)
    db.commit()
    assert row.result == F.RESULT_NOT_LINKED
    assert "전달되지 않았습니다" in row.detail


def test_linked_facility_reports_success(db, user):
    row = F.execute(db, facility=LINKED, command=F.CMD_ADVISE, user=user,
                    mode=F.MODE_ADVISE)
    db.commit()
    assert row.result == F.RESULT_OK


def test_control_records_mode_at_the_time(db, user):
    """나중에 모드가 바뀌어도 그때 어떤 모드였는지 알아야 한다."""
    row = F.execute(db, facility=LINKED, command=F.CMD_STATUS, user=user,
                    mode=F.MODE_REQUEST)
    db.commit()
    assert row.mode == F.MODE_REQUEST


def test_history_filters_by_facility(db, user):
    F.execute(db, facility=LINKED, command=F.CMD_STATUS, user=user,
              mode=F.MODE_ADVISE)
    F.execute(db, facility=UNLINKED, command=F.CMD_STATUS, user=user,
              mode=F.MODE_ADVISE)
    db.commit()
    assert len(F.history(db, facility_id="F-OK")) == 1
    assert len(F.history(db)) == 2


# --- 모드 저장 ---------------------------------------------------------------
def test_default_mode_is_advise(db):
    """되돌릴 수 없는 행위를 기본값으로 열어 두지 않는다."""
    from tot_dashboard.core import settings
    settings.invalidate()
    assert F.current_mode(db) == F.MODE_ADVISE


def test_mode_round_trip(db):
    F.set_mode(db, F.MODE_REQUEST)
    db.commit()
    assert F.current_mode(db) == F.MODE_REQUEST
    F.set_mode(db, F.MODE_ADVISE)
    db.commit()


def test_unknown_mode_is_rejected(db):
    with pytest.raises(ValueError):
        F.set_mode(db, "무엇이든")


def test_corrupt_stored_mode_falls_back_to_advise(db):
    """설정이 깨져 있어도 안전한 쪽으로 내려가야 한다."""
    from tot_dashboard.core import settings
    settings.set_value(db, F.KEY_MODE, "garbage")
    db.commit()
    assert F.current_mode(db) == F.MODE_ADVISE
    F.set_mode(db, F.MODE_ADVISE)
    db.commit()


# --- 권한 -------------------------------------------------------------------
def test_operator_can_advise_but_not_remote_control():
    """차단 「권고」는 알림의 변형이라 관제요원도 가능하다.
    원격 제어는 시설을 실제로 움직이므로 제외한다."""
    assert R.can(R.Role.OPR, R.FACILITY, R.Action.REQUEST) is True
    assert R.can(R.Role.OPR, R.FACILITY, R.Action.EXECUTE) is False


def test_only_sys_can_change_mode():
    assert R.can(R.Role.SYS, R.FACILITY_MODE, R.Action.EDIT) is True
    assert R.can(R.Role.MGR, R.FACILITY_MODE, R.Action.EDIT) is False
    assert R.can(R.Role.OPR, R.FACILITY_MODE, R.Action.EDIT) is False


# --- 설정 파일 ---------------------------------------------------------------
def test_shipped_config_marks_everything_unlinked():
    """실제 연동 규격을 확보하기 전까지는 전부 미연계여야 한다."""
    F.invalidate()
    facs = F.load(force=True)
    assert facs, "설정 템플릿이 비어 있습니다"
    assert all(f.get("link") == "none" for f in facs)


def test_missing_config_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("URBANGUARD_FACILITIES_PATH", str(tmp_path / "nope.json"))
    F.invalidate()
    assert F.load(force=True) == []
    monkeypatch.delenv("URBANGUARD_FACILITIES_PATH")
    F.invalidate()
