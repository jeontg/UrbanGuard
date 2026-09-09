"""내 계정 — 비밀번호 변경.

임시 비밀번호로 발급·초기화된 계정은 본인이 바꾸기 전까지 다른 화면을 쓸 수
없다(:mod:`..core.guard`). 발급자가 아는 비밀번호가 계속 살아 있으면 안 된다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import client_ip, current_user, get_db
from ..core.models import User
from ..core.security import (COOKIE_NAME, SESSION_MAX_AGE_SEC, hash_password,
                             issue_session, password_problem, verify_password)

log = logging.getLogger("urbanguard.account")


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, user: User, *, error: str = "",
              notice: str = "", status_code: int = 200):
        return templates.TemplateResponse(request, "account_password.html", {
            **base_ctx(request, user), "active": "account-password",
            "error": error, "notice": notice,
            "forced": user.must_change_password,
        }, status_code=status_code)

    @app.get("/account/password", response_class=HTMLResponse)
    def password_page(request: Request, user: User = Depends(current_user)):
        return _page(request, user)

    @app.post("/account/password", response_class=HTMLResponse)
    def password_change(request: Request,
                        current_password: str = Form(...),
                        new_password: str = Form(...),
                        confirm_password: str = Form(...),
                        db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
        ip = client_ip(request)

        if not verify_password(current_password, user.pw_hash):
            # 실패 사유를 감사 로그에 남긴다 — 탈취된 세션으로 비밀번호를
            # 바꾸려는 시도를 사후에 확인할 수 있어야 한다.
            audit.record_and_commit(db, action=audit.USER_PASSWORD_RESET,
                                    user=user, ip=ip,
                                    target="본인 비밀번호 변경 실패(현재 비밀번호 불일치)")
            return _page(request, user, error="현재 비밀번호가 올바르지 않습니다.",
                         status_code=400)

        if new_password != confirm_password:
            return _page(request, user, error="새 비밀번호가 서로 다릅니다.",
                         status_code=400)

        if new_password == current_password:
            return _page(request, user,
                         error="현재 비밀번호와 다른 값으로 정하세요.", status_code=400)

        problem = password_problem(new_password)
        if problem:
            return _page(request, user, error=problem, status_code=400)

        user.pw_hash = hash_password(new_password)
        user.pw_updated_at = datetime.now(timezone.utc)
        user.must_change_password = False
        user.failed_count = 0
        user.locked_until = None
        audit.record(db, action=audit.USER_PASSWORD_RESET, user=user, ip=ip,
                     target="본인 비밀번호 변경")
        db.commit()
        log.info("비밀번호 변경 login_id=%s ip=%s", user.login_id, ip)

        # 쿠키를 새로 발급한다. 서명 쿠키 방식이라 **다른 기기의 기존 세션은
        # 무효화되지 않는다** — 강제 로그아웃이 필요하면 서버측 세션 테이블
        # 도입이 선행되어야 한다(docs/ui_design_spec.md 11절).
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(COOKIE_NAME,
                        issue_session(user.id, user.login_id, user.role),
                        max_age=SESSION_MAX_AGE_SEC, httponly=True,
                        samesite="lax", secure=request.app.state.cookie_secure,
                        path="/")
        return resp
