"""가입 신청 — 로그인 화면 셀프서비스 신청 (2026-09-01 신설).

승인·거절은 여기 없다 — 관리자 화면(`routes_admin.py`, `/admin/users`)이
맡는다. 이 파일은 **누구나 로그인 없이** 열 수 있는 공개 경로 하나뿐이라,
새 검증(아이디 형식·중복·남용 방지)을 여기서 전부 끝내고 넘긴다.
"""
from __future__ import annotations

import re

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core import roles as R
from ..core import signup_requests as SR
from ..core.auth import client_ip, get_db
from ..core.models import User
from ..core.security import hash_password, password_problem

# 관리자 생성 경로(core/bootstrap.py)는 이 형식 검증을 거치지 않는다 —
# 그쪽은 내부 담당자가 직접 입력하므로 위험이 다르다. 공개 신청 경로만
# 새로 건다(영숫자·밑줄·붙임표, 3~32자).
LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def register(app, templates, base_ctx) -> None:

    def _form(request: Request, *, notice: str = "", error: str = "",
             status_code: int = 200):
        return templates.TemplateResponse(request, "signup.html", {
            **base_ctx(request), "roles": [(r.value, R.ROLE_LABELS[r])
                                           for r in R.Role],
            "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/signup", response_class=HTMLResponse)
    def signup_page(request: Request):
        return _form(request)

    @app.post("/signup", response_class=HTMLResponse)
    def signup_submit(request: Request, login_id: str = Form(...),
                      name: str = Form(...), dept: str = Form(""),
                      password: str = Form(...),
                      password_confirm: str = Form(...),
                      role: str = Form(R.Role.OPR.value),
                      domains: list[str] = Form(default=[]),
                      reason: str = Form(""),
                      db: Session = Depends(get_db)):
        login_id = login_id.strip()
        ip = client_ip(request)

        if not LOGIN_ID_RE.match(login_id):
            return _form(request, status_code=400,
                        error="아이디는 영문·숫자·밑줄(_)·붙임표(-) 3~32자여야 합니다.")
        if password != password_confirm:
            return _form(request, status_code=400,
                        error="비밀번호와 비밀번호 확인이 서로 다릅니다.")
        problem = password_problem(password)
        if problem:
            return _form(request, status_code=400, error=problem)
        try:
            role_value = R.Role(role).value
            domain_values = [R.Domain(d).value for d in domains]
        except ValueError:
            return _form(request, status_code=400,
                        error="역할 또는 도메인 값이 올바르지 않습니다.")
        if db.scalar(select(User).where(User.login_id == login_id)) is not None:
            return _form(request, status_code=400,
                        error=f"이미 사용 중인 아이디입니다: {login_id}")
        if SR.has_pending_login_id(db, login_id):
            return _form(request, status_code=400,
                        error="이미 접수된 신청이 있습니다. 관리자 승인을 기다려주세요.")
        # 최소한의 자체 제한(2026-09-01 사용자 결정 — CAPTCHA 등 외부
        # 라이브러리는 도입하지 않는다). 같은 IP에서 짧은 시간 안에
        # 반복 제출하면 막는다.
        if SR.too_many_recent_from(db, ip):
            return _form(request, status_code=429,
                        error="짧은 시간 안에 신청이 너무 많습니다. 잠시 후 다시 시도해주세요.")

        SR.submit(db, login_id=login_id, name=name.strip(), dept=dept.strip(),
                  requested_role=role_value, requested_domains=domain_values,
                  reason=reason.strip(), pw_hash=hash_password(password), ip=ip)
        audit.record_and_commit(
            db, action=audit.SIGNUP_REQUEST, login_id=login_id, ip=ip,
            target=login_id,
            after={"role": role_value, "domains": domain_values})
        return _form(request,
                    notice="가입 신청이 접수됐습니다. 관리자 승인 후 로그인할 수 "
                           "있습니다 — 승인 결과는 별도로 알려드리지 않으니, "
                           "잠시 후 로그인을 다시 시도해보세요.")
