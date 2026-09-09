"""로그인·로그아웃 (S-00).

설계서 5절 와이어프레임 기준. 로그인 실패는 아이디/비밀번호 중 무엇이 틀렸는지
구분해 알려주지 않는다 — 계정 존재 여부가 새어 나가면 대상 계정을 특정한
무차별 대입이 가능해진다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..core import audit
from ..core import signup_requests as SR
from ..core.auth import client_ip, get_db, optional_user
from ..core.models import User
from ..core.security import (COOKIE_NAME, MAX_FAILED, SESSION_MAX_AGE_SEC,
                             is_locked, issue_session, lock_deadline,
                             verify_password)

log = logging.getLogger("urbanguard.auth")
router = APIRouter()
LOGIN_FAIL_MSG = "아이디 또는 비밀번호가 올바르지 않습니다."


def _safe_next(raw: str | None) -> str:
    """열린 리다이렉트 방지. 같은 사이트의 절대경로만 허용한다."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def register(app, templates, base_ctx) -> None:
    """base_ctx(request) -> dict : 기관명·CSS 버전 등 모든 화면 공통 컨텍스트."""

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/",
                   db: Session = Depends(get_db)):
        if optional_user(request, db) is not None:
            return RedirectResponse(url=_safe_next(next), status_code=303)
        return templates.TemplateResponse(request, "login.html", {
            **base_ctx(request), "next": _safe_next(next), "error": None,
        })

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, login_id: str = Form(...),
                     password: str = Form(...), next: str = Form("/"),
                     db: Session = Depends(get_db)):
        ip = client_ip(request)
        target = _safe_next(next)

        def fail(msg: str, action: str):
            audit.record_and_commit(db, action=action, login_id=login_id,
                                    target=target, ip=ip)
            return templates.TemplateResponse(request, "login.html", {
                **base_ctx(request), "next": target, "error": msg,
            }, status_code=401)

        user = db.query(User).filter(User.login_id == login_id).one_or_none()
        if user is None or not user.is_active:
            # 2026-09-01 신설 — 계정이 없는 게 아니라 "가입 신청이 승인
            # 대기 중"인 경우를 구분해 알려준다. 계정 존재 여부를 흘리지
            # 않는다는 원칙과는 다른 층위다 — 신청은 신청자 본인이 방금
            # 자기 손으로 접수한 것이라, 그 상태를 알려줘도 새로 새는
            # 정보가 없다(진짜 계정의 존재 여부는 여전히 밝히지 않는다).
            if SR.has_pending_login_id(db, login_id):
                return fail("가입 신청이 접수돼 있고 아직 승인 대기 중입니다. "
                            "관리자 승인 후 다시 시도하세요.", audit.LOGIN_FAILURE)
            return fail(LOGIN_FAIL_MSG, audit.LOGIN_FAILURE)
        if is_locked(user.locked_until):
            return fail(f"계정이 잠겨 있습니다. {user.locked_until:%H:%M} 이후 다시 시도하세요.",
                        audit.LOGIN_LOCKED)
        if not verify_password(password, user.pw_hash):
            user.failed_count += 1
            if user.failed_count >= MAX_FAILED:
                user.locked_until = lock_deadline()
                user.failed_count = 0
                db.commit()
                return fail(f"실패 횟수를 초과해 계정이 잠겼습니다. "
                            f"{user.locked_until:%H:%M} 이후 다시 시도하세요.",
                            audit.LOGIN_LOCKED)
            db.commit()
            left = MAX_FAILED - user.failed_count
            return fail(f"{LOGIN_FAIL_MSG} ({left}회 남음)", audit.LOGIN_FAILURE)

        user.failed_count = 0
        user.locked_until = None
        user.last_login_at = datetime.now(timezone.utc)
        audit.record(db, action=audit.LOGIN_SUCCESS, user=user, ip=ip, target=target)
        db.commit()

        resp = RedirectResponse(url=target, status_code=303)
        resp.set_cookie(
            COOKIE_NAME, issue_session(user.id, user.login_id, user.role),
            max_age=SESSION_MAX_AGE_SEC, httponly=True, samesite="lax",
            # 운영은 HTTPS 전제. 개발(HTTP)에서도 쿠키가 붙도록 secure 는
            # 환경변수로 켠다 — 배포 시 URBANGUARD_COOKIE_SECURE=1 필수.
            secure=request.app.state.cookie_secure, path="/")
        log.info("로그인 성공 login_id=%s role=%s ip=%s", user.login_id, user.role, ip)
        return resp

    @app.post("/logout")
    def logout(request: Request, db: Session = Depends(get_db)):
        user = optional_user(request, db)
        if user is not None:
            audit.record_and_commit(db, action=audit.LOGOUT, user=user,
                                    ip=client_ip(request))
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp
