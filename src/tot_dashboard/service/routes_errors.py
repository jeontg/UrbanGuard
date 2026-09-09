"""오류 관리 화면 — 발생 이력(S-92), 오류 코드 사전(S-93).

권한은 **시스템관리자 전용**이다(사용자 지정). 오류 메시지에는 내부 경로·질의·
스택트레이스가 그대로 실려 오므로 열람 범위를 넓히면 시스템 내부 구조가 함께
퍼진다. 메뉴도 SYS 에게만 보인다(:func:`~..core.auth.menu_for`).

삭제는 **실제 삭제**다. 감사 로그(``audit_logs``)와 달리 오류 이력은 지운다.
다만 지운 행위는 감사 로그에 남긴다 — 무엇을 몇 건 지웠는지가 남아야
「장애 기록을 지운 것 아니냐」는 물음에 답할 수 있다.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..core import audit, errors
from ..core import error_catalog as catalog
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

# 감사 로그 행위 코드 — 오류 이력 삭제는 되돌릴 수 없으므로 반드시 남긴다.
ERROR_DELETE = "error.delete"
ERROR_RESOLVE = "error.resolve"
ERROR_CODE_SAVE = "error_code.save"
ERROR_CODE_DELETE = "error_code.delete"

SOURCE_LABELS = {"web": "화면·API", "worker": "상시 탐지", "pipeline": "분석",
                 "manual": "수동 기록"}


def register(app, templates, base_ctx) -> None:

    # ---------- S-92 오류 발생 이력 ----------
    def _errors_page(request: Request, db: Session, user: User, *,
                     notice: str = "", error: str = "", status_code: int = 200):
        # 검색 조건은 화면을 다시 그릴 때도 유지돼야 한다 — 삭제 후 조건이
        # 풀려 전체 목록으로 돌아가면, 방금 무엇을 보고 있었는지 잃는다.
        p = request.query_params
        filt = {"q": p.get("q", ""), "code": p.get("code", ""),
                "category": p.get("category", ""), "severity": p.get("severity", ""),
                "source": p.get("source", ""), "resolved": p.get("resolved", ""),
                "days": p.get("days", "")}
        try:
            days = int(filt["days"] or 0)
        except ValueError:
            days = 0
        rows = errors.search(db, q=filt["q"], code=filt["code"],
                             category=filt["category"], severity=filt["severity"],
                             source=filt["source"], resolved=filt["resolved"],
                             days=days)
        return templates.TemplateResponse(request, "admin_errors.html", {
            **base_ctx(request, user), "active": "errors", "rows": rows,
            "codes": errors.code_map(db), "summary": errors.summary(db),
            "categories": catalog.CATEGORIES, "severities": catalog.SEVERITIES,
            "sources": SOURCE_LABELS, "filt": filt, "limit": errors.DEFAULT_LIMIT,
            # 조치·삭제 후에도 보던 조건으로 되돌아가도록 폼 action 에 붙인다.
            "qs": urlencode([(k, v) for k, v in p.multi_items()
                             if k not in ("notice", "error")]),
            # 알림 문구는 리다이렉트로 넘어온다(_back). 직접 넘긴 값이 있으면 그쪽이 우선.
            "notice": notice or p.get("notice", ""),
            "error": error or p.get("error", ""),
        }, status_code=status_code)

    @app.get("/admin/errors", response_class=HTMLResponse)
    def errors_page(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                      R.Action.VIEW))):
        return _errors_page(request, db, user)

    def _back(request: Request, notice: str = "", error: str = ""):
        """검색 조건을 유지한 채 목록으로 돌아간다 (POST → 리다이렉트 → GET).

        새로고침할 때 삭제가 다시 실행되는 것을 막는다.
        """
        # 알림 문구는 한글이라 반드시 인코딩해야 한다 — 날것으로 붙이면
        # 주소가 깨져 조건까지 함께 날아간다.
        params = [(k, v) for k, v in request.query_params.multi_items()
                  if k not in ("notice", "error")]
        if notice:
            params.append(("notice", notice))
        elif error:
            params.append(("error", error))
        return RedirectResponse(url=f"/admin/errors?{urlencode(params)}",
                                status_code=303)

    @app.post("/admin/errors/resolve", response_class=HTMLResponse)
    def errors_resolve(request: Request, log_id: int = Form(...),
                       note: str = Form(""), undo: str = Form(""),
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                         R.Action.EDIT))):
        ok = errors.resolve(db, log_id, by=user.login_id, note=note,
                            undo=bool(undo))
        if not ok:
            return _back(request, error="대상 오류를 찾을 수 없습니다.")
        audit.record_and_commit(
            db, action=ERROR_RESOLVE, user=user, ip=client_ip(request),
            target=f"error_log:{log_id}",
            after={"resolved": not undo, "note": note[:200]})
        return _back(request, notice="처리 상태를 변경했습니다." if not undo
                     else "처리완료를 해제했습니다.")

    @app.post("/admin/errors/delete", response_class=HTMLResponse)
    def errors_delete(request: Request, log_ids: list[int] = Form(default=[]),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                        R.Action.EDIT))):
        if not log_ids:
            return _back(request, error="선택한 항목이 없습니다.")
        # 무엇을 지웠는지 남긴다. 삭제하면 원본이 없으므로 **지우기 전에** 읽는다.
        doomed = [errors.get(db, i) for i in log_ids]
        detail = [{"id": r.id, "code": r.code, "count": r.count,
                   "message": r.message[:120]} for r in doomed if r is not None]
        n = errors.delete(db, log_ids)
        audit.record_and_commit(
            db, action=ERROR_DELETE, user=user, ip=client_ip(request),
            target=f"error_log x{n}", before={"deleted": detail})
        return _back(request, notice=f"오류 이력 {n}건을 삭제했습니다.")

    @app.post("/admin/errors/cleanup", response_class=HTMLResponse)
    def errors_cleanup(request: Request, before_days: int = Form(30),
                       resolved_only: str = Form("1"),
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                         R.Action.EDIT))):
        """오래된 이력 일괄 정리. 기본은 「처리완료 + N일 이전」."""
        only = resolved_only == "1"
        try:
            n = errors.delete_by_filter(db, resolved_only=only,
                                        before_days=max(int(before_days), 1))
        except ValueError as e:
            return _back(request, error=str(e))
        audit.record_and_commit(
            db, action=ERROR_DELETE, user=user, ip=client_ip(request),
            target=f"error_log 일괄 x{n}",
            before={"before_days": before_days, "resolved_only": only, "count": n})
        scope = "처리완료 건 중 " if only else "처리 여부와 무관하게 "
        return _back(request,
                     notice=f"{scope}{before_days}일 이전 {n}건을 삭제했습니다.")

    # ---------- S-93 오류 코드 사전 ----------
    def _codes_page(request: Request, db: Session, user: User, *,
                    notice: str = "", error: str = "", status_code: int = 200,
                    form: dict | None = None):
        p = request.query_params
        filt = {"q": p.get("q", ""), "category": p.get("category", "")}
        rows = errors.codes(db, q=filt["q"], category=filt["category"])
        return templates.TemplateResponse(request, "admin_error_codes.html", {
            **base_ctx(request, user), "active": "error-codes", "rows": rows,
            "categories": catalog.CATEGORIES, "severities": catalog.SEVERITIES,
            "usage": errors.code_usage(db), "filt": filt,
            "form": form or {}, "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/admin/error-codes", response_class=HTMLResponse)
    def codes_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                     R.Action.VIEW))):
        return _codes_page(request, db, user)

    @app.post("/admin/error-codes/save", response_class=HTMLResponse)
    def codes_save(request: Request, code: str = Form(...),
                   category: str = Form("SYS"), title: str = Form(""),
                   severity: str = Form("error"), cause: str = Form(""),
                   resolution: str = Form(""), is_active: str = Form("1"),
                   db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                     R.Action.EDIT))):
        before = errors.get_code(db, code.strip().upper())
        snapshot = None
        if before is not None:
            snapshot = {"title": before.title, "severity": before.severity,
                        "cause": before.cause[:400],
                        "resolution": before.resolution[:400]}
        try:
            row = errors.save_code(db, code=code, category=category, title=title,
                                   severity=severity, cause=cause,
                                   resolution=resolution,
                                   is_active=(is_active == "1"),
                                   by=user.login_id)
        except ValueError as e:
            # 입력값을 그대로 돌려줘 다시 채우지 않아도 되게 한다.
            return _codes_page(request, db, user, error=str(e), status_code=400,
                               form={"code": code, "category": category,
                                     "title": title, "severity": severity,
                                     "cause": cause, "resolution": resolution,
                                     "is_active": is_active})
        audit.record_and_commit(
            db, action=ERROR_CODE_SAVE, user=user, ip=client_ip(request),
            target=row.code, before=snapshot,
            after={"title": row.title, "severity": row.severity})
        verb = "수정했습니다" if snapshot else "등록했습니다"
        return _codes_page(request, db, user, notice=f"{row.code} 를 {verb}.")

    @app.post("/admin/error-codes/delete", response_class=HTMLResponse)
    def codes_delete(request: Request, code: str = Form(...),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.SYSTEM_ERROR,
                                                       R.Action.EDIT))):
        try:
            ok = errors.delete_code(db, code.strip().upper())
        except ValueError as e:
            return _codes_page(request, db, user, error=str(e), status_code=400)
        if not ok:
            return _codes_page(request, db, user, error="대상 코드를 찾을 수 없습니다.",
                               status_code=404)
        audit.record_and_commit(
            db, action=ERROR_CODE_DELETE, user=user, ip=client_ip(request),
            target=code)
        return _codes_page(request, db, user, notice=f"{code} 를 삭제했습니다.")
