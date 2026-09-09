"""디지털 SOP 편집 화면 (S-86).

조치 단계를 정의하는 곳이다. 이행 체크는 이벤트 상세(S-03)에 있다
(:mod:`.routes_events`).

**「기본안 n/n」 표시가 이 화면의 핵심이다.** 제품이 심어 둔 뼈대를 기관
매뉴얼로 바꿨는지 숫자로 보여 주지 않으면, 아무도 바꾸지 않은 채 납품된다.
"""
from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..core import audit
from ..core import roles as R
from ..core import sop
from ..core.auth import client_ip, get_db, require_page
from ..core.models import SopStep, User

SOP_SAVE = "sop.save"
SOP_DELETE = "sop.delete"
SOP_SEED = "sop.seed"


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", error: str = "", status_code: int = 200,
              form: dict | None = None):
        p = request.query_params
        filt = {"domain": p.get("domain", ""), "level": p.get("level", "")}
        rows = sop.all_steps(db, domain=filt["domain"], level=filt["level"])
        stock, total = sop.builtin_ratio(db)
        return templates.TemplateResponse(request, "settings_sop.html", {
            **base_ctx(request, user), "active": "set-sop", "rows": rows,
            "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
            "levels": sop.SOP_LEVELS, "any_label": sop.ANY_LABEL,
            "stock": stock, "total": total, "filt": filt,
            "can_edit": R.can(user.role, R.SOP, R.Action.EDIT),
            "form": form or {},
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/settings/sop", response_class=HTMLResponse)
    def sop_page(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_page(R.SOP, R.Action.VIEW))):
        return _page(request, db, user)

    @app.post("/settings/sop/save", response_class=HTMLResponse)
    def sop_save(request: Request, step_id: str = Form(""),
                 domain: str = Form(""), level: str = Form(""),
                 title: str = Form(""), detail: str = Form(""),
                 seq: str = Form("0"), required: str = Form("1"),
                 is_active: str = Form("1"),
                 db: Session = Depends(get_db),
                 user: User = Depends(require_page(R.SOP, R.Action.EDIT))):
        raw = dict(step_id=step_id, domain=domain, level=level, title=title,
                   detail=detail, seq=seq, required=required,
                   is_active=is_active)
        try:
            sid = int(step_id) if step_id.strip() else None
        except ValueError:
            sid = None
        try:
            n = int(seq)
        except ValueError:
            # 순서를 숫자로 못 읽으면 맨 뒤로 보낸다 — 저장 자체를 막으면
            # 적어 둔 본문까지 잃는다.
            n = 999
        # 고치기 전 값을 감사 로그에 남긴다 — 단계 자체에는 이력이 없다.
        before = None
        if sid is not None:
            old = db.get(SopStep, sid)
            if old is not None:
                before = {"title": old.title, "domain": old.domain,
                          "level": old.level, "required": old.required,
                          "is_active": old.is_active}
        try:
            row = sop.save_step(db, step_id=sid, domain=domain, level=level,
                                title=title, detail=detail, seq=n,
                                required=(required == "1"),
                                is_active=(is_active == "1"),
                                by=user.login_id)
        except ValueError as e:
            return _page(request, db, user, error=str(e), status_code=400,
                         form=raw)
        audit.record_and_commit(
            db, action=SOP_SAVE, user=user, ip=client_ip(request),
            target=f"sop:{row.id}", before=before,
            after={"title": row.title, "domain": row.domain,
                   "level": row.level, "required": row.required,
                   "is_active": row.is_active})
        verb = "수정했습니다" if before else "등록했습니다"
        return _page(request, db, user, notice=f"「{row.title}」 단계를 {verb}.")

    @app.post("/settings/sop/delete", response_class=HTMLResponse)
    def sop_delete(request: Request, step_id: int = Form(...),
                   db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.SOP, R.Action.EDIT))):
        # 지우기 전에 읽는다 — 지운 뒤에는 무엇을 지웠는지 알 수 없다.
        old = db.get(SopStep, step_id)
        snapshot = None if old is None else {
            "title": old.title, "domain": old.domain, "level": old.level}
        if not sop.delete_step(db, step_id):
            return _page(request, db, user, error="대상 단계를 찾을 수 없습니다.",
                         status_code=404)
        audit.record_and_commit(
            db, action=SOP_DELETE, user=user, ip=client_ip(request),
            target=f"sop:{step_id}", before=snapshot)
        return _page(request, db, user,
                     notice="단계를 삭제했습니다. 이미 찍힌 이행 기록은 남습니다.")

    @app.post("/settings/sop/seed", response_class=HTMLResponse)
    def sop_seed(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_page(R.SOP, R.Action.EDIT))):
        """기본 뼈대를 다시 심는다. **덮어쓰지 않는다** — 고쳐 둔 단계는
        그대로 두고, 없는 것만 채운다."""
        n = sop.seed_builtin(db)
        audit.record_and_commit(
            db, action=SOP_SEED, user=user, ip=client_ip(request),
            target="sop 기본안", after={"added": n})
        return _page(request, db, user,
                     notice=(f"기본안 {n}개를 추가했습니다." if n
                             else "추가할 기본안이 없습니다. 이미 모두 있습니다."))
