"""영상 반출 관리대장 화면 (S-94).

**삭제 라우트가 없다.** 일부러다. 대장은 감사 자료라 지우는 길을 만들지
않는다(:mod:`..core.video_disclosure` 참고). 잘못 적었으면 정정본을 만든다.

권한은 SYS 전체, MGR 등록·정정까지, OPR 열람만이다. 반출 요청이 실제로 오는
곳이 부서라 MGR 을 등록 주체로 뒀다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..core import audit
from ..core import roles as R
from ..core import video_disclosure as vd
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

# 감사 로그 행위 코드. 대장 자체가 기록이지만, **대장을 누가 만졌는지**는
# 감사 로그가 답한다 — 대장만 보면 「등록자」가 진짜 등록한 사람인지 알 수 없다.
DISCLOSURE_CREATE = "disclosure.create"
DISCLOSURE_CORRECT = "disclosure.correct"
DISCLOSURE_DISPOSE = "disclosure.dispose"


def _dt(raw: str | None) -> datetime | None:
    """``datetime-local`` 입력값을 읽는다. 못 읽으면 ``None`` — 폼에서 비워 둔
    칸과 잘못 적은 칸을 구분하지 않는다. 필수 칸은 :mod:`..core.video_disclosure`
    가 따로 막는다."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", error: str = "", status_code: int = 200,
              form: dict | None = None):
        p = request.query_params
        filt = {"q": p.get("q", ""), "method": p.get("method", ""),
                "overdue": p.get("overdue", ""), "days": p.get("days", "")}
        try:
            days = int(filt["days"] or 0)
        except ValueError:
            days = 0
        rows = vd.search(db, q=filt["q"], method=filt["method"],
                         overdue=filt["overdue"], days=days)
        # 정정본이 있는 줄은 흐리게 보여 준다. 목록에 있는 것만 확인하면 되므로
        # 한 번에 모아 조회한다 — 줄마다 질의하면 200줄에 200번 나간다.
        ids = {r.id for r in rows}
        corrected = {r.corrects_id for r in rows if r.corrects_id in ids}
        return templates.TemplateResponse(request, "admin_disclosure.html", {
            **base_ctx(request, user), "active": "disclosure", "rows": rows,
            "summary": vd.summary(db), "methods": vd.METHODS,
            "basis_presets": vd.BASIS_PRESETS,
            "cam_labels": vd.camera_labels(db), "corrected": corrected,
            "is_overdue": vd.is_overdue, "filt": filt,
            "can_edit": R.can(user.role, R.VIDEO_DISCLOSURE, R.Action.EDIT),
            "form": form or {},
            "notice": notice or p.get("notice", ""),
            "error": error or p.get("error", ""),
        }, status_code=status_code)

    @app.get("/admin/disclosure", response_class=HTMLResponse)
    def disclosure_page(request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_page(R.VIDEO_DISCLOSURE,
                                                          R.Action.VIEW))):
        return _page(request, db, user)

    def _back(request: Request, notice: str = "", error: str = ""):
        """검색 조건을 유지한 채 목록으로 (POST → 리다이렉트 → GET).

        새로고침으로 같은 반출이 두 줄 등록되는 것을 막는다.
        """
        params = [(k, v) for k, v in request.query_params.multi_items()
                  if k not in ("notice", "error")]
        if notice:
            params.append(("notice", notice))
        elif error:
            params.append(("error", error))
        qs = urlencode(params)
        return RedirectResponse(url=f"/admin/disclosure{'?' + qs if qs else ''}",
                                status_code=303)

    # 필수 칸(요청 기관·반출 근거·정정 사유)도 ``Form("")`` 으로 받는다.
    # ``Form(...)`` 로 두면 값이 비었을 때 FastAPI 가 422 로 먼저 막아, 우리
    # 안내문 대신 영문 오류가 뜨고 적어 둔 나머지 칸까지 날아간다. 검증은
    # :mod:`..core.video_disclosure` 한 곳에서만 한다.
    def _fields(**kw) -> dict:
        """폼 값을 코어 모듈 인자로 옮긴다. 등록·정정이 같은 칸을 쓴다."""
        return {
            "requested_at": _dt(kw["requested_at"]),
            "requester_org": kw["requester_org"],
            "requester_name": kw["requester_name"],
            "requester_contact": kw["requester_contact"],
            "legal_basis": kw["legal_basis"], "purpose": kw["purpose"],
            "camera_ids": kw["camera_ids"],
            "period_from": _dt(kw["period_from"]),
            "period_to": _dt(kw["period_to"]),
            "method": kw["method"], "masked": kw["masked"] == "1",
            "handled_at": _dt(kw["handled_at"]),
            "handler_login": kw["handler_login"],
            "disposal_due": _dt(kw["disposal_due"]),
            "note": kw["note"],
        }

    @app.post("/admin/disclosure/create", response_class=HTMLResponse)
    def disclosure_create(
            request: Request,
            requested_at: str = Form(""), requester_org: str = Form(""),
            requester_name: str = Form(""), requester_contact: str = Form(""),
            legal_basis: str = Form(""), purpose: str = Form(""),
            camera_ids: str = Form(""), period_from: str = Form(""),
            period_to: str = Form(""), method: str = Form("view"),
            masked: str = Form("1"), handled_at: str = Form(""),
            handler_login: str = Form(""), disposal_due: str = Form(""),
            note: str = Form(""),
            db: Session = Depends(get_db),
            user: User = Depends(require_page(R.VIDEO_DISCLOSURE,
                                              R.Action.EDIT))):
        raw = dict(requested_at=requested_at, requester_org=requester_org,
                   requester_name=requester_name,
                   requester_contact=requester_contact,
                   legal_basis=legal_basis, purpose=purpose,
                   camera_ids=camera_ids, period_from=period_from,
                   period_to=period_to, method=method, masked=masked,
                   handled_at=handled_at, handler_login=handler_login,
                   disposal_due=disposal_due, note=note)
        try:
            row = vd.create(db, by=user.login_id, handler_id=user.id,
                            **_fields(**raw))
        except ValueError as e:
            # 적은 것을 돌려준다. 반출 대장은 칸이 많아 다시 채우게 하면
            # 「나중에 적자」가 되고, 그 나중이 오지 않는다.
            return _page(request, db, user, error=str(e), status_code=400,
                         form=raw)
        audit.record_and_commit(
            db, action=DISCLOSURE_CREATE, user=user, ip=client_ip(request),
            target=f"disclosure:{row.id}",
            after={"org": row.requester_org, "basis": row.legal_basis,
                   "method": row.method, "masked": row.masked})
        return _back(request, notice=f"반출 기록 {row.id}번을 등록했습니다.")

    @app.post("/admin/disclosure/correct", response_class=HTMLResponse)
    def disclosure_correct(
            request: Request, origin_id: int = Form(...),
            correction_reason: str = Form(""),
            requested_at: str = Form(""), requester_org: str = Form(""),
            requester_name: str = Form(""), requester_contact: str = Form(""),
            legal_basis: str = Form(""), purpose: str = Form(""),
            camera_ids: str = Form(""), period_from: str = Form(""),
            period_to: str = Form(""), method: str = Form("view"),
            masked: str = Form("1"), handled_at: str = Form(""),
            handler_login: str = Form(""), disposal_due: str = Form(""),
            note: str = Form(""),
            db: Session = Depends(get_db),
            user: User = Depends(require_page(R.VIDEO_DISCLOSURE,
                                              R.Action.EDIT))):
        raw = dict(requested_at=requested_at, requester_org=requester_org,
                   requester_name=requester_name,
                   requester_contact=requester_contact,
                   legal_basis=legal_basis, purpose=purpose,
                   camera_ids=camera_ids, period_from=period_from,
                   period_to=period_to, method=method, masked=masked,
                   handled_at=handled_at, handler_login=handler_login,
                   disposal_due=disposal_due, note=note)
        # 정정 전 값을 감사 로그에 남긴다. 대장에도 원본이 남지만, 무엇이
        # 바뀌었는지는 두 줄을 비교해야 알 수 있어 여기 요약해 둔다.
        origin = vd.get(db, origin_id)
        before = None if origin is None else {
            "org": origin.requester_org, "basis": origin.legal_basis,
            "method": origin.method, "masked": origin.masked}
        try:
            row = vd.correct(db, origin_id, reason=correction_reason,
                             by=user.login_id, handler_id=user.id,
                             **_fields(**raw))
        except ValueError as e:
            return _page(request, db, user, error=str(e), status_code=400,
                         form={**raw, "origin_id": origin_id,
                               "correction_reason": correction_reason})
        audit.record_and_commit(
            db, action=DISCLOSURE_CORRECT, user=user, ip=client_ip(request),
            target=f"disclosure:{origin_id}→{row.id}", before=before,
            after={"org": row.requester_org, "basis": row.legal_basis,
                   "method": row.method, "masked": row.masked,
                   "reason": row.correction_reason[:200]})
        return _back(request,
                     notice=f"{origin_id}번을 정정해 {row.id}번으로 남겼습니다.")

    @app.post("/admin/disclosure/dispose", response_class=HTMLResponse)
    def disclosure_dispose(request: Request, row_id: int = Form(...),
                           db: Session = Depends(get_db),
                           user: User = Depends(require_page(
                               R.VIDEO_DISCLOSURE, R.Action.EDIT))):
        if not vd.dispose(db, row_id):
            return _back(request, error="대상 기록을 찾을 수 없습니다.")
        audit.record_and_commit(
            db, action=DISCLOSURE_DISPOSE, user=user, ip=client_ip(request),
            target=f"disclosure:{row_id}", after={"disposed": True})
        return _back(request, notice=f"{row_id}번을 파기 완료로 표시했습니다.")
