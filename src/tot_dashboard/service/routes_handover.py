"""교대 인수인계 화면 (S-04).

한 화면에서 끝난다 — 쓰다 만 인계문 이어쓰기, 확인 대기 목록, 지난 기록이
같은 페이지에 있다. 교대 시각은 바쁘고, 화면을 옮겨 다니게 하면 안 쓴다.

**삭제 라우트가 없다.** 인계 기록을 지울 수 있으면 「전달 못 받았다」는 다툼에
답할 근거가 사라진다(:mod:`..core.handover` 머리말).
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core import handover as H
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

HANDOVER_SUBMIT = "handover.submit"
HANDOVER_ACK = "handover.ack"


def _scope(user: User) -> set[str] | None:
    """MGR 만 담당 도메인으로 제한된다 — 스냅샷에 담기는 이벤트 범위다.
    (:mod:`.routes_events` 의 ``_scope`` 와 같은 규칙.)"""
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", error: str = "", status_code: int = 200,
              form: dict | None = None):
        p = request.query_params
        filt = {"status": p.get("status", ""), "q": p.get("q", ""),
                "days": p.get("days", "")}
        try:
            days = int(filt["days"] or 0)
        except ValueError:
            days = 0
        # 지금 쓸 인계문에 붙을 이벤트를 미리 보여 준다 — 「무엇이 넘어가는가」를
        # 제출 전에 알아야 요약을 제대로 쓸 수 있다.
        preview = H.snapshot_open_events(db, allowed_domains=_scope(user))
        return templates.TemplateResponse(request, "handover.html", {
            **base_ctx(request, user), "active": "handover",
            "rows": H.search(db, status=filt["status"], q=filt["q"], days=days),
            "pending": H.pending_ack(db, exclude_user_id=user.id),
            "draft": H.my_draft(db, user.id),
            "preview": preview,
            "counts": H.summary_counts(db),
            "status_labels": H.STATUS_LABELS, "presets": H.SHIFT_PRESETS,
            "domain_labels": R.DOMAIN_LABELS,
            "open_items": H.open_items,
            "operators": db.scalars(
                select(User.login_id).where(User.is_active.is_(True))
                .order_by(User.login_id)).all(),
            "can_edit": R.can(user.role, R.HANDOVER, R.Action.EDIT),
            "can_ack": R.can(user.role, R.HANDOVER, R.Action.APPROVE),
            "me": user.login_id, "my_id": user.id,
            "filt": filt, "form": form or {},
            "notice": notice or p.get("notice", ""),
            "error": error or p.get("error", ""),
        }, status_code=status_code)

    @app.get("/handover", response_class=HTMLResponse)
    def handover_page(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.HANDOVER,
                                                        R.Action.VIEW))):
        return _page(request, db, user)

    def _back(request: Request, notice: str = "", error: str = ""):
        """POST → 리다이렉트 → GET. 새로고침으로 인계문이 두 번 넘어가는 것을
        막는다."""
        params = [(k, v) for k, v in request.query_params.multi_items()
                  if k not in ("notice", "error")]
        if notice:
            params.append(("notice", notice))
        elif error:
            params.append(("error", error))
        qs = urlencode(params)
        return RedirectResponse(url=f"/handover{'?' + qs if qs else ''}",
                                status_code=303)

    def _resolve_to(db: Session, to_login: str) -> int | None:
        """인수자 로그인 ID → 사용자 ID. 못 찾으면 이름만 적어 둔다 —
        아직 계정이 없는 사람에게 넘기는 일이 실제로 있다."""
        to_login = (to_login or "").strip()
        if not to_login:
            return None
        return db.scalars(select(User.id)
                          .where(User.login_id == to_login)).first()

    @app.post("/handover/save", response_class=HTMLResponse)
    def handover_save(request: Request, row_id: str = Form(""),
                      shift_name: str = Form(""), summary: str = Form(""),
                      todo: str = Form(""), to_login: str = Form(""),
                      submit: str = Form(""),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.HANDOVER,
                                                        R.Action.EDIT))):
        raw = dict(row_id=row_id, shift_name=shift_name, summary=summary,
                   todo=todo, to_login=to_login)
        scope = _scope(user)
        try:
            rid = int(row_id) if row_id.strip() else None
        except ValueError:
            rid = None
        try:
            if rid is None:
                row = H.create(db, shift_name=shift_name, summary=summary,
                               todo=todo, to_login=to_login,
                               to_user_id=_resolve_to(db, to_login),
                               user=user, allowed_domains=scope)
            else:
                row = H.update(db, rid, shift_name=shift_name, summary=summary,
                               todo=todo, to_login=to_login,
                               to_user_id=_resolve_to(db, to_login),
                               allowed_domains=scope)
            if submit == "1":
                H.submit(db, row.id, allowed_domains=scope)
        except ValueError as e:
            db.rollback()
            # 적은 것을 돌려준다 — 교대 시각에 다시 치게 하면 안 쓴다.
            return _page(request, db, user, error=str(e), status_code=400,
                         form=raw)

        if submit == "1":
            audit.record_and_commit(
                db, action=HANDOVER_SUBMIT, user=user, ip=client_ip(request),
                target=f"handover:{row.id}",
                after={"shift": row.shift_name, "to": row.to_login,
                       "open_events": (row.open_events or {}).get("count", 0)})
            n = (row.open_events or {}).get("count", 0)
            return _back(request,
                         notice=f"인계했습니다. 미처리 이벤트 {n}건이 함께 넘어갔습니다.")
        db.commit()
        return _back(request, notice="작성 내용을 저장했습니다. 아직 인계되지 않았습니다.")

    @app.post("/handover/ack", response_class=HTMLResponse)
    def handover_ack(request: Request, row_id: int = Form(...),
                     note: str = Form(""),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.HANDOVER,
                                                       R.Action.APPROVE))):
        try:
            row = H.acknowledge(db, row_id, user=user, note=note)
        except ValueError as e:
            db.rollback()
            return _back(request, error=str(e))
        audit.record_and_commit(
            db, action=HANDOVER_ACK, user=user, ip=client_ip(request),
            target=f"handover:{row.id}",
            after={"shift": row.shift_name, "from": row.from_login,
                   "note": row.ack_note[:200]})
        return _back(request, notice=f"{row.shift_name} 근무조 인계를 확인했습니다.")
