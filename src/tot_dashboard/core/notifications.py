"""알림 발송 요청·승인 (S-50 / S-51).

설계서 8절 「알림 3계층」 중 **①내부 통보**와 **③주민 경보**를 다룬다.
②기관 통보(112·119)는 관제요원 단독 권한이라 이벤트 상세에서 즉시 기록되고
여기를 거치지 않는다 — 승인 절차를 넣으면 즉시 통보 의무와 충돌한다.

**되돌릴 수 없는 행위는 기본값으로 열어 두지 않는다**는 원칙에 따라, 주민 경보는
2인 승인이 기본이다. 「심각」 단계 단독 발송은 설정으로만 열린다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import roles as R
from .models import Notification

log = logging.getLogger("urbanguard.notify")

# 상태
REQUESTED = "requested"   # 승인 대기
APPROVED = "approved"     # 승인됨(발송 직전)
SENT = "sent"             # 발송 완료
REJECTED = "rejected"     # 반려
FAILED = "failed"         # 발송 실패
STATUS_LABELS = {REQUESTED: "승인 대기", APPROVED: "승인됨", SENT: "발송 완료",
                 REJECTED: "반려", FAILED: "발송 실패"}
PENDING = (REQUESTED,)

# 계층 (설계서 8절)
TIER_DEPT = "dept"        # ① 내부 통보 — 담당 부서
TIER_RESIDENT = "resident"  # ③ 주민 경보 — CBS 재난문자
TIER_LABELS = {TIER_DEPT: "부서 통보", TIER_RESIDENT: "주민 경보"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def request(db: Session, *, event_id: int | None, domain: str, tier: str,
            body: str, user, risk_level: str = "",
            recipients_count: int = 0, channel: str = "sms") -> Notification:
    """발송 요청 생성.

    요청자가 스스로 승인할 수 있는지(MGR·SYS)는 :func:`can_self_approve` 로
    판단해 호출자가 이어서 처리한다. 여기서는 상태를 만들기만 한다.
    """
    n = Notification(case_id=str(event_id or ""), domain=domain,
                     risk_level=risk_level, channel=channel, body=body,
                     recipients_count=recipients_count, status=REQUESTED,
                     requested_by=getattr(user, "id", None),
                     requested_at=_now())
    # tier 는 별도 컬럼이 없어 case_id 와 함께 구분한다 — 주민 경보는
    # 채널이 CBS 로 고정되므로 채널로 계층을 판정할 수 있다.
    n.channel = "cbs" if tier == TIER_RESIDENT else channel
    db.add(n)
    db.flush()
    log.info("알림 요청 id=%s tier=%s by=%s", n.id, tier, getattr(user, "login_id", "?"))
    return n


def tier_of(n: Notification) -> str:
    return TIER_RESIDENT if n.channel == "cbs" else TIER_DEPT


def can_self_approve(user, tier: str, risk_level: str = "", *,
                     db: Session | None = None) -> bool:
    """요청자가 곧바로 승인·발송까지 할 수 있는가.

    - 부서 통보(①)는 되돌릴 수 있으므로 누구든 단독 진행 가능
    - 주민 경보(③)는 되돌릴 수 없으므로 **역할과 무관하게 2인 승인**이다.
      부서담당자·시스템관리자도 자기가 요청한 건은 스스로 승인할 수 없다 —
      그러면 「2인 승인」이 1인이 되어 제도가 무력해진다
    - 유일한 예외는 「심각」 단계 단독 발송 설정을 켠 경우이며, 이때도
      사후 승인 대상으로 표시된다

    ⚠️ 승인자가 한 명뿐인 기관에서는 주민 경보가 막힐 수 있다. 운영 인원
    구성은 도입 시 확인이 필요하다(설계서 11절 9번).
    """
    if tier == TIER_DEPT:
        return True
    return solo_send_allowed(risk_level, db=db)


def solo_send_allowed(risk_level: str = "", *, db: Session | None = None) -> bool:
    """「심각」 단독 발송 설정이 켜져 있고 등급이 심각인가.

    ``db`` 를 주면 **어휘표(``risk_levels.is_critical``)를 먼저 본다.** 기관이
    등급 이름을 「위험」으로 바꿔 놓아도 표만 고치면 되고 코드를 안 고쳐도 된다
    (경남 제안요청서 SFR-012 「최소 4단계 이상」 대응).

    ⚠️ **표가 비어 있으면 하드코딩 기본값으로 되돌아간다.** 빈 집합을
    「아무도 허용 안 함」으로 읽으면, 어휘를 아직 안 심은 설치본에서 심각
    상황에 단독 발송이 막힌다 — 안전한 쪽으로 넘어지는 것처럼 보이지만
    실제로는 **골든타임을 잃는다.**
    """
    if not R.solo_send_on_critical_enabled():
        return False
    allowed = R.CRITICAL_LEVELS
    if db is not None:
        try:
            from . import vocabulary as V
            from_table = V.critical_labels(db)
            if from_table:
                allowed = from_table
        except Exception:  # noqa: BLE001
            # 어휘표가 아직 없는 설치본(마이그레이션 전)에서도 돌아야 한다.
            pass
    return (risk_level or "") in allowed


def approve(db: Session, n: Notification, user, *, post_approval: bool = False) -> None:
    n.status = APPROVED
    n.approved_by = getattr(user, "id", None)
    n.approved_at = _now()
    n.needs_post_approval = post_approval


def reject(db: Session, n: Notification, user, reason: str) -> None:
    n.status = REJECTED
    n.approved_by = getattr(user, "id", None)
    n.approved_at = _now()
    n.reject_reason = (reason or "")[:255]


def mark_sent(db: Session, n: Notification, *, ok: bool = True,
              reason: str = "") -> None:
    n.status = SENT if ok else FAILED
    n.sent_at = _now()
    if not ok:
        n.reject_reason = (reason or "")[:255]


def list_pending(db: Session, allowed_domains: set[str] | None = None,
                 limit: int = 100) -> list[Notification]:
    stmt = select(Notification).where(Notification.status.in_(PENDING))
    if allowed_domains is not None:
        stmt = stmt.where(Notification.domain.in_(allowed_domains or {"__none__"}))
    return list(db.scalars(
        stmt.order_by(Notification.requested_at).limit(limit)).all())


def list_history(db: Session, *, status: str = "",
                 allowed_domains: set[str] | None = None,
                 limit: int = 200) -> list[Notification]:
    stmt = select(Notification)
    if status:
        stmt = stmt.where(Notification.status == status)
    if allowed_domains is not None:
        stmt = stmt.where(Notification.domain.in_(allowed_domains or {"__none__"}))
    return list(db.scalars(
        stmt.order_by(Notification.requested_at.desc()).limit(limit)).all())


def pending_count(db: Session, allowed_domains: set[str] | None = None) -> int:
    stmt = select(Notification.id).where(Notification.status.in_(PENDING))
    if allowed_domains is not None:
        stmt = stmt.where(Notification.domain.in_(allowed_domains or {"__none__"}))
    return len(list(db.scalars(stmt).all()))
