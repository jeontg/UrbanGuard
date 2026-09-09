"""감사 추적 기록.

설계서 1절 「모든 조치는 기록」 원칙. 로그인·설정변경·알림발송·보고서출력을
남긴다. 감사 기록 실패가 본래 업무를 막아서는 안 되므로, 기록 중 예외는
삼켜서 로깅만 하고 호출자에게 올리지 않는다.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .models import AuditLog

log = logging.getLogger("urbanguard.audit")

# 행위 코드. 문자열 오타를 막기 위해 상수로 둔다.
LOGIN_SUCCESS = "login.success"
LOGIN_FAILURE = "login.failure"
LOGIN_LOCKED = "login.locked"
LOGOUT = "logout"
USER_CREATE = "user.create"
USER_UPDATE = "user.update"
USER_DEACTIVATE = "user.deactivate"
USER_PASSWORD_RESET = "user.password_reset"
SIGNUP_REQUEST = "signup.request"
SIGNUP_APPROVE = "signup.approve"
SIGNUP_REJECT = "signup.reject"
NOTIFY_REQUEST = "notify.request"
NOTIFY_APPROVE = "notify.approve"
NOTIFY_REJECT = "notify.reject"
NOTIFY_SEND = "notify.send"
NOTIFY_SOLO_SEND = "notify.solo_send"     # 「심각」 단계 단독 발송 — 사후 승인 대상
REPORT_EXPORT = "report.export"
SETTINGS_UPDATE = "settings.update"
DETECT_RUN = "detect.run"
MODEL_TRAIN_START = "model.train_start"
MODEL_TRAIN_STOP = "model.train_stop"
# 2026-09-02 신설(이벤트 자동 보류 정책) — EVENT_CLOSE는 지금까지 사람이
# 이벤트를 종결해도 전역 감사 로그에 안 남던 공백을 함께 메운다
# (`EventAction`에는 이미 남고 있었다 — S-61은 그 이력을 몰랐을 뿐).
EVENT_CLOSE = "event.close"
EVENT_AUTO_HOLD = "event.auto_hold"


def record(db: Session, *, action: str, user=None, target: str = "",
           before: dict | None = None, after: dict | None = None,
           ip: str = "", login_id: str = "", dept: str = "") -> None:
    """감사 로그 1건을 남긴다. 커밋은 호출자 트랜잭션에 맡긴다."""
    try:
        db.add(AuditLog(
            user_id=getattr(user, "id", None),
            login_id=login_id or getattr(user, "login_id", "") or "",
            dept=dept or getattr(user, "dept", "") or "",
            action=action, target=target[:255],
            before=before, after=after, ip=ip[:64],
        ))
    except Exception:  # noqa: BLE001
        # 감사 기록 실패로 재난 대응 업무가 멈추면 안 된다.
        log.exception("감사 로그 기록 실패: action=%s target=%s", action, target)


def record_and_commit(db: Session, **kw) -> None:
    record(db, **kw)
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("감사 로그 커밋 실패")
        db.rollback()
