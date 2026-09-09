"""S-80 감시지점 관리 · S-81 ROI 설정.

두 화면이 함께 있어야 「지자체가 스스로 지점을 늘린다」가 성립한다. 지점만
등록하고 ROI를 못 그리면 그 지점의 물 면적 판정이 부정확한 채로 남는다.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..core import audit, blocks_edit as BE
from ..core import roi_edit as RE
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

log = logging.getLogger("urbanguard.blocks")


def register(app, templates, base_ctx, store) -> None:

    # ---------- S-80 목록 ----------
    def _list_page(request: Request, user: User, *, notice: str = "",
                   errors: list[str] | None = None, status_code: int = 200):
        blocks = BE.load()
        roi = RE.status([b.get("id") for b in blocks])
        rows = []
        for b in blocks:
            src = b.get("source") or {}
            c = b.get("coordinates") or {}
            rows.append({
                "id": b.get("id"), "name": b.get("name"),
                "dept": b.get("dept") or "—",
                "lat": c.get("lat"), "lng": c.get("lng"),
                "source_type": src.get("type") or "—",
                "source_label": BE.SOURCE_TYPES.get(src.get("type"), src.get("type") or "—"),
                "roi": roi.get(b.get("id"), {}),
                "has_crowd": bool(b.get("crowd")),
            })
        return templates.TemplateResponse(request, "settings_blocks.html", {
            **base_ctx(request, user), "active": "set-blocks",
            "rows": rows, "notice": notice, "errors": errors or [],
            "source_types": sorted(BE.SOURCE_TYPES.items()),
            "can_edit": R.can(user.role, R.SETTINGS_OPS, R.Action.EDIT),
            "roi_missing": [r for r in rows if not r["roi"].get("road")],
        }, status_code=status_code)

    @app.get("/settings/blocks", response_class=HTMLResponse)
    def blocks_page(request: Request,
                    user: User = Depends(require_page(R.SETTINGS_OPS,
                                                      R.Action.VIEW))):
        return _list_page(request, user)

    def _form(f) -> dict:
        return {k: (v if isinstance(v, str) else "") for k, v in f.items()}

    @app.post("/settings/blocks/create", response_class=HTMLResponse)
    async def blocks_create(request: Request, db: Session = Depends(get_db),
                            user: User = Depends(require_page(R.SETTINGS_OPS,
                                                              R.Action.EDIT))):
        data = _form(await request.form())
        block, errs, bak = BE.create(data)
        if errs:
            return _list_page(request, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"감시지점 추가 {block['id']}",
                     after={"name": block["name"],
                            "source": (block.get("source") or {}).get("type")})
        db.commit()
        return _list_page(request, user, notice=(
            f"{block['name']}({block['id']}) 지점을 추가했습니다. "
            "⚠ 탐지에 반영하려면 서비스를 재시작해야 하고, ROI 설정도 필요합니다."))

    @app.post("/settings/blocks/{block_id}/update", response_class=HTMLResponse)
    async def blocks_update(block_id: str, request: Request,
                            db: Session = Depends(get_db),
                            user: User = Depends(require_page(R.SETTINGS_OPS,
                                                              R.Action.EDIT))):
        data = _form(await request.form())
        before = BE.get(block_id)
        block, errs, bak = BE.update(block_id, data)
        if errs:
            return _list_page(request, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"감시지점 수정 {block_id}",
                     before={"name": (before or {}).get("name")},
                     after={"name": block["name"]})
        db.commit()
        return _list_page(request, user, notice=(
            f"{block['name']} 지점을 수정했습니다. ⚠ 반영하려면 서비스 재시작이 필요합니다."))

    @app.post("/settings/blocks/{block_id}/delete", response_class=HTMLResponse)
    def blocks_delete(block_id: str, request: Request,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.SETTINGS_OPS,
                                                        R.Action.EDIT))):
        before = BE.get(block_id)
        ok, errs, bak = BE.delete(block_id)
        if not ok:
            return _list_page(request, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"감시지점 삭제 {block_id}",
                     before={"name": (before or {}).get("name")})
        db.commit()
        return _list_page(request, user, notice=(
            f"{block_id} 지점을 삭제했습니다. ROI 파일은 남아 있습니다 — "
            "되살릴 때 다시 쓸 수 있습니다."))

    # ---------- S-81 ROI 편집 ----------
    @app.get("/settings/roi/{block_id}", response_class=HTMLResponse)
    def roi_page(block_id: str, request: Request,
                 user: User = Depends(require_page(R.SETTINGS_OPS,
                                                   R.Action.VIEW))):
        block = BE.get(block_id)
        if block is None:
            return RedirectResponse(url="/settings/blocks", status_code=303)
        snap = ""
        try:
            cur = store.get(block_id) or {}
            snap = cur.get("snapshot") or ""
        except Exception:  # noqa: BLE001
            snap = ""
        return templates.TemplateResponse(request, "settings_roi.html", {
            **base_ctx(request, user), "active": "set-blocks",
            "block": block, "roi": RE.load(block_id),
            "snapshot": snap, "layers": RE.LAYERS,
            "can_edit": R.can(user.role, R.SETTINGS_OPS, R.Action.EDIT),
        })

    @app.post("/settings/roi/{block_id}")
    async def roi_save(block_id: str, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_OPS,
                                                         R.Action.EDIT))):
        if BE.get(block_id) is None:
            return {"ok": False, "errors": ["지점을 찾을 수 없습니다."]}
        payload = await request.json()
        data, errs, bak = RE.save(block_id, payload)
        if errs:
            return {"ok": False, "errors": errs}
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"ROI 설정 {block_id}",
                     after={"road": len(data["road_roi"]),
                            "low": len(data["low_point_roi"]),
                            "line": bool(data["lane_threshold_line"])})
        db.commit()
        return {"ok": True,
                "message": "저장했습니다. 탐지에 반영하려면 서비스 재시작이 필요합니다.",
                "backup": bak.name if bak else None}
