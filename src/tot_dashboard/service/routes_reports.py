"""S-43 현장 제보 접수·검토.

⚠️ **시민 공개 제보는 아직 열지 않았다.** 무인증 업로드는 용량 제한·악용 차단·
스팸 대응이 함께 있어야 하고, 그 없이 열면 서버가 곧바로 표적이 된다.
현재는 로그인한 순찰원·담당자가 현장에서 올리는 경로만 제공한다.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from ..core import audit, blocks_edit as BE
from ..core import events as E
from ..core import image_mask
from ..core import reports as RP
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import CitizenReport, User

log = logging.getLogger("urbanguard.reports")


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *, status: str = "open",
              notice: str = "", errors: list[str] | None = None,
              status_code: int = 200):
        return templates.TemplateResponse(request, "reports.html", {
            **base_ctx(request, user), "active": "reports",
            "rows": RP.list_reports(db, status=status),
            "counts": RP.counts(db), "sel_status": status,
            "status_labels": RP.STATUS_LABELS,
            "mask_labels": image_mask.STATUS_LABELS,
            "needs_review": image_mask.NEEDS_REVIEW,
            "training_ready": RP.training_ready(db),
            "blocks": [(b.get("id"), b.get("name")) for b in BE.load()],
            "can_review": R.can(user.role, R.DETECT, R.Action.EXECUTE,
                                domain=R.Domain.ROAD,
                                user_domains=user.domain_set),
            "max_mb": RP.MAX_BYTES // 1024 // 1024,
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request, status: str = "open",
                     db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.MONITOR, R.Action.VIEW,
                                                       domain=R.Domain.ROAD))):
        return _page(request, db, user, status=status)

    @app.post("/reports", response_class=HTMLResponse)
    async def reports_create(request: Request, photo: UploadFile = File(...),
                             place_name: str = Form(""), block_id: str = Form(""),
                             description: str = Form(""), lat: str = Form(""),
                             lng: str = Form(""),
                             db: Session = Depends(get_db),
                             user: User = Depends(require_page(R.MONITOR,
                                                               R.Action.VIEW,
                                                               domain=R.Domain.ROAD))):
        blob = await photo.read()
        errs = RP.validate_upload(photo.filename or "", len(blob))
        if not (place_name.strip() or block_id.strip()):
            errs.append("지점을 선택하거나 위치를 입력하세요.")
        if errs:
            return _page(request, db, user, errors=errs, status_code=400)

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        tmp = Path(tempfile.gettempdir()) / f"ug_upload_{photo.filename}"
        try:
            tmp.write_bytes(blob)
            row = RP.create(db, raw_path=tmp, filename=photo.filename or "photo.jpg",
                            user=user, place_name=place_name, block_id=block_id,
                            description=description, lat=_f(lat), lng=_f(lng))
        finally:
            tmp.unlink(missing_ok=True)

        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"현장 제보 접수 #{row.id}",
                     after={"mask": row.mask_status, "masked_regions": row.mask_count})
        db.commit()

        msg = f"제보 #{row.id} 를 접수했습니다."
        if row.mask_status in image_mask.NEEDS_REVIEW:
            msg += (" ⚠ 사람 자동 마스킹이 적용되지 않았습니다 — "
                    "담당자가 사진을 확인한 뒤 사용하세요.")
        elif row.mask_count:
            msg += f" 사람 영역 {row.mask_count}곳을 가렸습니다."
        msg += " 차량번호판은 자동으로 가려지지 않으니 확인이 필요합니다."
        return _page(request, db, user, notice=msg)

    @app.get("/reports/photo/{name}")
    def report_photo(name: str,
                     user: User = Depends(require_page(R.MONITOR, R.Action.VIEW,
                                                       domain=R.Domain.ROAD))):
        p = RP.safe_photo_path(name)
        if p is None:
            return HTMLResponse("사진을 찾을 수 없습니다.", status_code=404)
        return FileResponse(p)

    def _load(db: Session, rid: int) -> CitizenReport | None:
        return db.get(CitizenReport, rid)

    @app.post("/reports/{rid}/action", response_class=HTMLResponse)
    def report_action(rid: int, request: Request, act: str = Form(...),
                      memo: str = Form(""), usable: str = Form(""),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.DETECT, R.Action.EXECUTE,
                                                        domain=R.Domain.ROAD))):
        row = _load(db, rid)
        if row is None:
            return _page(request, db, user, errors=["제보를 찾을 수 없습니다."],
                         status_code=404)
        memo = memo.strip()
        ip = client_ip(request)

        if act == "review":
            RP.review(db, row, user, usable=(usable == "1"), memo=memo)
            msg = ("확인 처리했습니다. "
                   + ("학습 데이터로 표시했습니다." if usable == "1"
                      else "학습에는 쓰지 않습니다."))
        elif act == "convert":
            # 보수 요청은 이벤트로 만든다 — 관제요원이 다루는 단위가 이벤트다.
            ev = E.record_detection(
                db, domain=R.Domain.ROAD.value,
                block_id=row.block_id or f"REPORT-{row.id}",
                place_name=row.place_name or "제보 지점", level="주의",
                event_type="도로 노면(제보)",
                detail={"제보 번호": row.id, "제보자": row.login_id,
                        "설명": row.description[:120]})
            RP.convert(db, row, user, ev.id if ev else None, memo)
            msg = (f"보수 요청으로 전환했습니다. 이벤트 #{ev.id} 가 생성됐습니다."
                   if ev else "보수 요청으로 전환했습니다.")
        elif act == "reject":
            RP.reject(db, row, user, memo)
            msg = "반려했습니다."
        elif act == "delete_photo":
            RP.delete_photo(row)
            msg = "사진을 파기했습니다. 되돌릴 수 없습니다."
        else:
            return _page(request, db, user, errors=["알 수 없는 조치입니다."],
                         status_code=400)

        audit.record(db, action=audit.SETTINGS_UPDATE, user=user, ip=ip,
                     target=f"현장 제보 #{row.id} {act}",
                     after={"status": row.status, "memo": memo})
        db.commit()
        return _page(request, db, user, notice=msg)
