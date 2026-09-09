"""가입 신청 — 로그인 화면 셀프서비스 신청 → 관리자 승인 (2026-09-01 신설).

``core/notifications.py``(S-50 알림 승인 — 요청/승인/거절 상태 + 감사로그
패턴)와 정확히 같은 모양을 그대로 따른다. 새 자원(roles.py)을 만들지
않는다 — 관리자 화면(사용자·권한 관리)에 딸린 기능이라 기존 ``USERS``
자원(시스템관리자만 ``Action.APPROVE``를 가짐)을 그대로 쓴다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import SignupRequest

log = logging.getLogger("urbanguard.signup")

# 상태
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATUS_LABELS = {PENDING: "승인 대기", APPROVED: "승인됨", REJECTED: "거절됨"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_pending_login_id(db: Session, login_id: str) -> bool:
    """이미 같은 아이디로 대기 중인 신청이 있는가 — 재접수를 막는다."""
    return db.scalar(
        select(SignupRequest.id)
        .where(SignupRequest.login_id == login_id,
              SignupRequest.status == PENDING)
        .limit(1)) is not None


def too_many_recent_from(db: Session, ip: str, *, window_min: int = 30,
                         limit: int = 3) -> bool:
    """같은 IP에서 최근 ``window_min``분 안에 ``limit``건 이상 제출했는가.

    ⚠️ 최소한의 자체 제한이다(2026-09-01 사용자 결정 — 외부 CAPTCHA
    라이브러리는 이번 범위에 넣지 않는다). IP 하나 뒤에 여러 사람이 있는
    사내망 환경에선 다소 거칠 수 있으나, "새 공개 엔드포인트인데 남용
    방지 장치가 전혀 없는 것"보다는 낫다.
    """
    if not ip:
        return False
    since = _now() - timedelta(minutes=window_min)
    count = db.scalar(
        select(func.count(SignupRequest.id))
        .where(SignupRequest.ip == ip, SignupRequest.created_at >= since))
    return (count or 0) >= limit


def submit(db: Session, *, login_id: str, name: str, dept: str,
          requested_role: str, requested_domains: list[str],
          reason: str, pw_hash: str, ip: str) -> SignupRequest:
    req = SignupRequest(
        login_id=login_id, name=name, dept=dept,
        requested_role=requested_role,
        requested_domains=",".join(requested_domains),
        reason=(reason or "")[:2000], pw_hash=pw_hash, ip=(ip or "")[:64],
        status=PENDING)
    db.add(req)
    db.flush()
    log.info("가입 신청 id=%s login_id=%s role=%s", req.id, login_id,
             requested_role)
    return req


def approve(db: Session, req: SignupRequest, admin, *,
           created_user_id: int) -> None:
    req.status = APPROVED
    req.reviewed_by = getattr(admin, "id", None)
    req.reviewed_at = _now()
    req.created_user_id = created_user_id


def reject(db: Session, req: SignupRequest, admin, reason: str) -> None:
    req.status = REJECTED
    req.reviewed_by = getattr(admin, "id", None)
    req.reviewed_at = _now()
    req.reject_reason = (reason or "")[:2000]


def list_pending(db: Session, limit: int = 200) -> list[SignupRequest]:
    return list(db.scalars(
        select(SignupRequest).where(SignupRequest.status == PENDING)
        .order_by(SignupRequest.created_at, SignupRequest.id)
        .limit(limit)).all())


def list_history(db: Session, *, status: str = "",
                 limit: int = 200) -> list[SignupRequest]:
    stmt = select(SignupRequest)
    if status:
        stmt = stmt.where(SignupRequest.status == status)
    # id 를 2차 키로 둔다 — 같은 초에 여러 신청이 들어오면 created_at
    # 하나만으로는 순서가 실행마다 뒤집힌다(tests/core/test_stable_
    # ordering.py 가 이 실수를 확실히 잡는다).
    return list(db.scalars(
        stmt.order_by(SignupRequest.created_at.desc(),
                      SignupRequest.id.desc()).limit(limit)).all())
