"""S-50 알림 승인 대기함 · S-51 통보 이력.

발송 **요청**은 이벤트 상세(S-03)에서 한다. 이 화면은 부서담당자가
**승인만 처리하는 대기함**이다(설계서 5절).
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..core import audit, notifications as N
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import Notification, User

log = logging.getLogger("urbanguard.notify")

STATUS_BADGE = {N.REQUESTED: "ug-badge--warn", N.APPROVED: "ug-badge--on",
                N.SENT: "ug-badge--on", N.REJECTED: "ug-badge--off",
                N.FAILED: "ug-badge--crit"}


def _scope(user: User) -> set[str] | None:
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def _dry_run() -> bool:
    return os.environ.get("NOTIFICATION_DRY_RUN", "true").lower() != "false"


def register(app, templates, base_ctx) -> None:

    # ---------- S-50 승인 대기함 ----------
    def _pending_page(request: Request, db: Session, user: User, *,
                      notice: str = "", error: str = "", status_code: int = 200):
        rows = N.list_pending(db, _scope(user))
        return templates.TemplateResponse(request, "notify_pending.html", {
            **base_ctx(request, user), "active": "notify",
            "rows": rows, "tier_of": N.tier_of,
            "tier_labels": N.TIER_LABELS, "status_labels": N.STATUS_LABELS,
            "status_badge": STATUS_BADGE, "domain_labels": R.DOMAIN_LABELS,
            "can_approve": R.can(user.role, R.NOTIFY, R.Action.APPROVE),
            "dry_run": _dry_run(),
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/notify", response_class=HTMLResponse)
    def notify_pending(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.NOTIFY, R.Action.VIEW))):
        return _pending_page(request, db, user)

    def _load(db: Session, user: User, nid: int) -> Notification | None:
        n = db.get(Notification, nid)
        if n is None:
            return None
        scope = _scope(user)
        if scope is not None and n.domain not in scope:
            return None
        return n

    @app.post("/notify/{nid}/approve", response_class=HTMLResponse)
    def notify_approve(nid: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.NOTIFY,
                                                         R.Action.APPROVE))):
        n = _load(db, user, nid)
        if n is None:
            return _pending_page(request, db, user, error="대상을 찾을 수 없습니다.",
                                 status_code=404)
        if n.status != N.REQUESTED:
            return _pending_page(request, db, user,
                                 error="이미 처리된 요청입니다.", status_code=400)
        if n.requested_by == user.id and N.tier_of(n) == N.TIER_RESIDENT:
            # 2인 승인의 핵심 — 요청자가 자기 요청을 승인하면 의미가 없다.
            # 역할과 무관하게 막는다(부서담당자·시스템관리자 포함).
            return _pending_page(
                request, db, user, status_code=403,
                error="본인이 요청한 주민 경보는 본인이 승인할 수 없습니다. "
                      "다른 승인권자에게 요청하세요.")

        N.approve(db, n, user)
        audit.record(db, action=audit.NOTIFY_APPROVE, user=user,
                     ip=client_ip(request),
                     target=f"알림 #{n.id} {N.TIER_LABELS.get(N.tier_of(n), '')}")

        # ⚠️ 실제 발송은 아직 붙이지 않았다. SOLAPI 실발송 검증(P1-4)과
        # CBS 연계(설계서 11절 4번)가 선행되어야 한다. 지금은 상태만 진행시킨다.
        if _dry_run():
            N.mark_sent(db, n, ok=True)
            msg = "승인했습니다. (발송 차단 모드 — 실제 문자는 나가지 않았습니다)"
        else:
            N.mark_sent(db, n, ok=False, reason="실발송 연동 미구현")
            msg = "승인했으나 실발송 연동이 아직 없어 「발송 실패」로 남겼습니다."
        audit.record(db, action=audit.NOTIFY_SEND, user=user,
                     ip=client_ip(request), target=f"알림 #{n.id}",
                     after={"status": n.status, "dry_run": _dry_run()})
        db.commit()
        log.info("알림 승인 id=%s by=%s status=%s", n.id, user.login_id, n.status)
        return _pending_page(request, db, user, notice=msg)

    @app.post("/notify/{nid}/reject", response_class=HTMLResponse)
    def notify_reject(nid: int, request: Request, reason: str = Form(""),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.NOTIFY,
                                                        R.Action.APPROVE))):
        n = _load(db, user, nid)
        if n is None:
            return _pending_page(request, db, user, error="대상을 찾을 수 없습니다.",
                                 status_code=404)
        if n.status != N.REQUESTED:
            return _pending_page(request, db, user,
                                 error="이미 처리된 요청입니다.", status_code=400)
        N.reject(db, n, user, reason.strip() or "사유 미기재")
        audit.record(db, action=audit.NOTIFY_REJECT, user=user,
                     ip=client_ip(request), target=f"알림 #{n.id}",
                     after={"reason": n.reject_reason})
        db.commit()
        return _pending_page(request, db, user, notice="반려했습니다.")

    # ---------- S-51 통보 이력 ----------
    @app.get("/notify/history", response_class=HTMLResponse)
    def notify_history(request: Request, status: str = "",
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.NOTIFY_HISTORY,
                                                         R.Action.VIEW))):
        rows = N.list_history(db, status=status, allowed_domains=_scope(user))
        return templates.TemplateResponse(request, "notify_history.html", {
            **base_ctx(request, user), "active": "notify-history",
            "rows": rows, "tier_of": N.tier_of, "tier_labels": N.TIER_LABELS,
            "status_labels": N.STATUS_LABELS, "status_badge": STATUS_BADGE,
            "domain_labels": R.DOMAIN_LABELS, "sel_status": status,
            "statuses": sorted(N.STATUS_LABELS.items(), key=lambda kv: kv[1]),
        })
