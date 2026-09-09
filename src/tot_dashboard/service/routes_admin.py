"""관리자 화면 — 사용자·권한 관리(S-60), 감사 로그(S-61).

권한은 설계서 4절대로 S-60은 SYS 전용, S-61은 SYS 전체 / MGR은 본인 부서만이다.
"""
from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import account_recovery as recovery
from ..core import audit
from ..core import roles as R
from ..core import signup_requests as SR
from ..core.auth import client_ip, get_db, require_page
from ..core.bootstrap import create_user, generate_password
from ..core.models import AuditLog, SignupRequest, User, UserDomain

AUDIT_ACTION_LABELS = {
    audit.LOGIN_SUCCESS: "로그인 성공", audit.LOGIN_FAILURE: "로그인 실패",
    audit.LOGIN_LOCKED: "계정 잠금", audit.LOGOUT: "로그아웃",
    audit.USER_CREATE: "계정 생성", audit.USER_UPDATE: "계정 수정",
    audit.USER_DEACTIVATE: "계정 비활성화", audit.USER_PASSWORD_RESET: "비밀번호 초기화",
    audit.SIGNUP_REQUEST: "가입 신청", audit.SIGNUP_APPROVE: "가입 승인",
    audit.SIGNUP_REJECT: "가입 거절",
    audit.NOTIFY_REQUEST: "알림 발송 요청", audit.NOTIFY_APPROVE: "알림 발송 승인",
    audit.NOTIFY_REJECT: "알림 발송 반려", audit.NOTIFY_SEND: "알림 발송",
    audit.NOTIFY_SOLO_SEND: "알림 단독 발송(사후승인 대상)",
    audit.REPORT_EXPORT: "보고서 출력", audit.SETTINGS_UPDATE: "설정 변경",
    audit.DETECT_RUN: "AI 탐지 실행",
    # 잠금 해제는 초기화와 다른 행위다 — 비밀번호를 바꾸지 않는다.
    recovery.USER_UNLOCK: "계정 잠금 해제",
    # 오류 관리(S-92·S-93). 오류 이력은 실제로 지워지므로, 누가 무엇을 몇 건
    # 지웠는지는 여기에만 남는다.
    "error.delete": "오류 이력 삭제", "error.resolve": "오류 처리 표시",
    "error_code.save": "오류 코드 등록·수정", "error_code.delete": "오류 코드 삭제",
    # 영상 반출 관리대장(S-94). 대장 자체가 기록이지만, **대장을 누가 만졌는지**는
    # 여기에만 남는다 — 대장의 「등록자」가 진짜 등록한 사람인지는 대장이 모른다.
    "disclosure.create": "영상 반출 등록", "disclosure.correct": "영상 반출 정정",
    "disclosure.dispose": "영상 반출 파기 확인",
    # 디지털 SOP(S-86). 조치 절차가 언제 어떻게 바뀌었는지는 여기에만 남는다.
    "sop.save": "SOP 단계 등록·수정", "sop.delete": "SOP 단계 삭제",
    "sop.seed": "SOP 기본안 채우기",
    # 교대 인수인계(S-04). 「전달 못 받았다」는 다툼에 답할 근거다.
    "handover.submit": "교대 인계", "handover.ack": "교대 인수 확인",
}


def register(app, templates, base_ctx) -> None:

    # ---------- S-60 사용자·권한 관리 ----------
    def _users_page(request: Request, db: Session, user: User, *,
                    notice: str = "", error: str = "", status_code: int = 200):
        rows = db.scalars(select(User).order_by(User.role, User.login_id)).all()
        return templates.TemplateResponse(request, "admin_users.html", {
            **base_ctx(request, user), "active": "users", "rows": rows,
            "roles": [(r.value, R.ROLE_LABELS[r]) for r in R.Role],
            "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
            # 2026-09-01 신설 — 가입 신청 대기함. 로그인 화면 셀프서비스
            # 신청을 여기서 검토·승인한다(role.USERS는 SYS 전용이라 이
            # 화면을 이미 보고 있다는 것 자체가 승인 권한이 있다는 뜻).
            "pending_signups": SR.list_pending(db),
            "role_labels": R.ROLE_LABELS, "domain_labels": R.DOMAIN_LABELS,
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.USERS, R.Action.VIEW))):
        return _users_page(request, db, user)

    @app.post("/admin/users/create", response_class=HTMLResponse)
    def users_create(request: Request, login_id: str = Form(...),
                     name: str = Form(...), dept: str = Form(""),
                     role: str = Form(R.Role.OPR.value),
                     domains: list[str] = Form(default=[]),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.USERS, R.Action.EDIT))):
        login_id = login_id.strip()
        if db.scalar(select(User).where(User.login_id == login_id)) is not None:
            return _users_page(request, db, user,
                               error=f"이미 존재하는 아이디입니다: {login_id}",
                               status_code=400)
        password = generate_password()
        try:
            created = create_user(db, login_id=login_id, name=name.strip(),
                                  dept=dept.strip(), role=R.Role(role).value,
                                  password=password, domains=domains)
        except ValueError as e:
            return _users_page(request, db, user, error=str(e), status_code=400)
        audit.record(db, action=audit.USER_CREATE, user=user, ip=client_ip(request),
                     target=login_id,
                     after={"role": role, "dept": dept, "domains": domains})
        db.commit()
        return _users_page(request, db, user,
                           notice=f"{created.name}({login_id}) 계정을 만들었습니다. "
                                  f"임시 비밀번호: {password} — 이 값은 다시 표시되지 않습니다.")

    @app.post("/admin/users/{user_id}/toggle", response_class=HTMLResponse)
    def users_toggle(user_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.USERS, R.Action.EDIT))):
        target = db.get(User, user_id)
        if target is None:
            return _users_page(request, db, user, error="대상 계정을 찾을 수 없습니다.",
                               status_code=404)
        if target.id == user.id:
            return _users_page(request, db, user,
                               error="본인 계정은 비활성화할 수 없습니다.", status_code=400)
        before = target.is_active
        target.is_active = not before
        audit.record(db, action=audit.USER_DEACTIVATE, user=user,
                     ip=client_ip(request), target=target.login_id,
                     before={"is_active": before}, after={"is_active": target.is_active})
        db.commit()
        state = "활성화" if target.is_active else "비활성화"
        return _users_page(request, db, user, notice=f"{target.login_id} 계정을 {state}했습니다.")

    @app.post("/admin/users/{user_id}/reset", response_class=HTMLResponse)
    def users_reset(user_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_page(R.USERS, R.Action.EDIT))):
        target = db.get(User, user_id)
        if target is None:
            return _users_page(request, db, user, error="대상 계정을 찾을 수 없습니다.",
                               status_code=404)
        # 실제 동작은 복구 모듈이 맡는다 — 서버 CLI 와 같은 코드를 써야
        # 「화면에서는 실패 횟수를 안 지운다」 같은 어긋남이 생기지 않는다.
        try:
            result = recovery.reset_password(db, target.login_id, actor_user=user,
                                             ip=client_ip(request))
        except ValueError as e:
            return _users_page(request, db, user, error=str(e), status_code=400)
        extra = ""
        if result.was_locked or result.failed_count:
            extra = f" (잠겨 있던 계정이라 잠금도 함께 해제했습니다)"
        return _users_page(request, db, user,
                           notice=f"{target.login_id} 비밀번호를 초기화했습니다.{extra} "
                                  f"임시 비밀번호: {result.password} — 이 값은 다시 표시되지 않습니다.")

    @app.post("/admin/users/{user_id}/unlock", response_class=HTMLResponse)
    def users_unlock(user_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.USERS, R.Action.EDIT))):
        """잠금만 푼다. **비밀번호는 그대로 둔다.**

        초기화만 있으면, 오타로 잠긴 사람의 멀쩡한 비밀번호까지 버리게 된다.
        잠금 사유의 대부분이 단순 오타라 이 경로가 훨씬 자주 쓰인다.
        """
        target = db.get(User, user_id)
        if target is None:
            return _users_page(request, db, user, error="대상 계정을 찾을 수 없습니다.",
                               status_code=404)
        was = recovery.unlock(db, target.login_id, actor_user=user,
                              ip=client_ip(request))
        return _users_page(request, db, user,
                           notice=f"{target.login_id} 계정의 잠금을 해제했습니다."
                           if was else
                           f"{target.login_id} 계정은 잠겨 있지 않았습니다.")

    @app.post("/admin/users/{user_id}/role", response_class=HTMLResponse)
    def users_role(user_id: int, request: Request, role: str = Form(...),
                   domains: list[str] = Form(default=[]),
                   db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.USERS, R.Action.EDIT))):
        target = db.get(User, user_id)
        if target is None:
            return _users_page(request, db, user, error="대상 계정을 찾을 수 없습니다.",
                               status_code=404)
        before = {"role": target.role, "domains": sorted(target.domain_set)}
        target.role = R.Role(role).value
        target.domains.clear()
        db.flush()
        for d in domains:
            db.add(UserDomain(user_id=target.id, domain=R.Domain(d).value))
        audit.record(db, action=audit.USER_UPDATE, user=user, ip=client_ip(request),
                     target=target.login_id, before=before,
                     after={"role": role, "domains": sorted(domains)})
        db.commit()
        return _users_page(request, db, user,
                           notice=f"{target.login_id} 권한을 변경했습니다.")

    # ---------- 가입 신청 승인/거절 (2026-09-01 신설) ----------
    @app.post("/admin/users/signup/{req_id}/approve", response_class=HTMLResponse)
    def signup_approve(req_id: int, request: Request, role: str = Form(...),
                       domains: list[str] = Form(default=[]),
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.USERS,
                                                         R.Action.APPROVE))):
        req = db.get(SignupRequest, req_id)
        if req is None:
            return _users_page(request, db, user, error="대상 신청을 찾을 수 없습니다.",
                               status_code=404)
        if req.status != SR.PENDING:
            return _users_page(request, db, user,
                               error="이미 처리된 신청입니다.", status_code=400)
        # 승인 사이 다른 경로로 같은 아이디가 먼저 등록됐을 수 있다 —
        # 신청 시점이 아니라 **승인 시점**에 다시 확인해야 한다.
        if db.scalar(select(User).where(User.login_id == req.login_id)) is not None:
            return _users_page(
                request, db, user, status_code=400,
                error=f"이미 사용 중인 아이디입니다: {req.login_id} — "
                      "신청을 거절하고 다른 아이디로 다시 신청받아야 합니다.")
        try:
            role_value = R.Role(role).value
            domain_values = [R.Domain(d).value for d in domains]
        except ValueError:
            return _users_page(request, db, user,
                               error="역할 또는 도메인 값이 올바르지 않습니다.",
                               status_code=400)
        # 신청 당시 이미 검증·해시된 비밀번호를 그대로 옮긴다 — 관리자는
        # 비밀번호를 모르고, 신청자는 자기가 정한 비밀번호를 그대로 쓴다.
        created = create_user(db, login_id=req.login_id, name=req.name,
                              dept=req.dept, role=role_value,
                              pw_hash=req.pw_hash, domains=domain_values,
                              must_change=False)
        db.flush()
        SR.approve(db, req, user, created_user_id=created.id)
        audit.record(db, action=audit.SIGNUP_APPROVE, user=user,
                     ip=client_ip(request), target=req.login_id,
                     after={"role": role_value, "domains": domain_values,
                           "requested_role": req.requested_role})
        audit.record(db, action=audit.USER_CREATE, user=user,
                     ip=client_ip(request), target=req.login_id,
                     after={"role": role_value, "dept": req.dept,
                           "domains": domain_values, "via": "signup"})
        db.commit()
        return _users_page(request, db, user,
                           notice=f"{req.name}({req.login_id}) 가입 신청을 승인해 "
                                  "계정을 만들었습니다.")

    @app.post("/admin/users/signup/{req_id}/reject", response_class=HTMLResponse)
    def signup_reject(req_id: int, request: Request, reason: str = Form(""),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.USERS,
                                                        R.Action.APPROVE))):
        req = db.get(SignupRequest, req_id)
        if req is None:
            return _users_page(request, db, user, error="대상 신청을 찾을 수 없습니다.",
                               status_code=404)
        if req.status != SR.PENDING:
            return _users_page(request, db, user,
                               error="이미 처리된 신청입니다.", status_code=400)
        SR.reject(db, req, user, reason.strip() or "사유 미기재")
        audit.record(db, action=audit.SIGNUP_REJECT, user=user,
                     ip=client_ip(request), target=req.login_id,
                     after={"reason": req.reject_reason})
        db.commit()
        return _users_page(request, db, user,
                           notice=f"{req.name}({req.login_id}) 가입 신청을 거절했습니다.")

    # ---------- S-61 감사 로그 ----------
    @app.get("/admin/audit", response_class=HTMLResponse)
    def audit_page(request: Request, action: str = "", q: str = "",
                   db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.AUDIT, R.Action.VIEW))):
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(300)
        # 설계서 4절 주석 6 — MGR 은 본인 부서 로그만 볼 수 있다.
        if user.role != R.Role.SYS.value:
            stmt = stmt.where(AuditLog.dept == user.dept)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if q:
            stmt = stmt.where(AuditLog.login_id.ilike(f"%{q}%"))
        rows = db.scalars(stmt).all()
        return templates.TemplateResponse(request, "admin_audit.html", {
            **base_ctx(request, user), "active": "audit", "rows": rows,
            "labels": AUDIT_ACTION_LABELS, "sel_action": action, "q": q,
            "actions": sorted(AUDIT_ACTION_LABELS.items(), key=lambda kv: kv[1]),
            "scoped": user.role != R.Role.SYS.value,
        })
