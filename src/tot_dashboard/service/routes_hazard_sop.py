"""S-98 위험유형별 SOP 관리 — `hazard_sop_map` 을 사람이 다루는 자리.

**왜 필요한가.** 표를 만들어 두고 **읽는 코드도 화면도 없었다.** 표만 있으면
아무도 채우지 못하고, 채워지지 않으면 있으나 마나다.

경남 제안요청서 **SFR-012** 「단계별 SOP 를 자동으로 **도, 시·군별로** 제시」의
「도, 시·군별」이 ``zone_id`` 다.

⚠️ **이 화면은 단계를 만들지 않는다.** 단계는 S-86 에서 만들고, 여기서는
**어느 위험유형·등급·구역에 그 단계를 붙일지**만 정한다. 두 화면이 같은 일을
하면 어디서 고쳐야 하는지 아무도 모르게 된다.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core import roles as R
from ..core import sop as SOP
from ..core.auth import client_ip, get_db, require_page
from ..core.models import HazardSopMap, HazardType, SopStep, User, Zone

log = logging.getLogger("urbanguard.hazard_sop")


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", errors: list[str] | None = None,
              status_code: int = 200):
        # 탐지 가능한 유형만 고르게 한다. 산불·태풍까지 띄우면 화면이
        # 「우리가 그것도 탐지한다」고 말하는 셈이 된다.
        types = list(db.scalars(
            select(HazardType).where(HazardType.is_active.is_(True))
            .order_by(HazardType.domain, HazardType.code)))
        steps = list(db.scalars(
            select(SopStep).where(SopStep.is_active.is_(True))
            .order_by(SopStep.domain, SopStep.level, SopStep.seq)))
        zones = list(db.scalars(select(Zone).order_by(Zone.path)))
        rows = list(db.scalars(
            select(HazardSopMap).order_by(HazardSopMap.hazard_type_code,
                                          HazardSopMap.level_code,
                                          HazardSopMap.seq)))
        by_type = {t.code: t for t in types}
        by_step = {s.id: s for s in steps}
        table = [{
            "id": r.id,
            "hazard_code": r.hazard_type_code,
            "hazard_label": (by_type[r.hazard_type_code].label
                             if r.hazard_type_code in by_type
                             else r.hazard_type_code),
            "level": r.level_code or SOP.ANY_LABEL,
            "zone": r.zone_id or SOP.ANY_LABEL,
            "step_title": (by_step[r.sop_step_id].title
                           if r.sop_step_id in by_step else "(없는 단계)"),
            "step_missing": r.sop_step_id not in by_step,
            "seq": r.seq,
        } for r in rows]
        return templates.TemplateResponse(request, "settings_hazard_sop.html", {
            **base_ctx(request, user), "active": "set-hazard-sop",
            "types": types, "steps": steps, "zones": zones, "rows": table,
            "levels": SOP.SOP_LEVELS, "any_label": SOP.ANY_LABEL,
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/settings/hazard-sop", response_class=HTMLResponse)
    def hazard_sop_page(request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_page(R.SOP,
                                                          R.Action.VIEW))):
        return _page(request, db, user)

    @app.post("/settings/hazard-sop/add", response_class=HTMLResponse)
    def hazard_sop_add(request: Request,
                       hazard_type_code: str = Form(""),
                       level_code: str = Form(""),
                       zone_id: str = Form(""),
                       sop_step_id: int = Form(0),
                       seq: int = Form(0),
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SOP,
                                                         R.Action.EDIT))):
        code = (hazard_type_code or "").strip()
        level = (level_code or "").strip()
        zone = (zone_id or "").strip()
        errs: list[str] = []

        if not code:
            errs.append("위험유형을 고르십시오.")
        elif db.get(HazardType, code) is None:
            errs.append(f"없는 위험유형입니다: {code}")
        step = db.get(SopStep, sop_step_id) if sop_step_id else None
        if step is None:
            errs.append("붙일 SOP 단계를 고르십시오.")
        if zone and db.get(Zone, zone) is None:
            errs.append(f"없는 구역입니다: {zone}")

        # ⚠️ 같은 조합을 두 번 넣으면 유일 제약에 걸려 500 이 난다. 미리 막고
        #    사람이 읽을 수 있는 말로 알린다.
        if not errs:
            dup = db.scalars(select(HazardSopMap).where(
                HazardSopMap.hazard_type_code == code,
                HazardSopMap.level_code == level,
                HazardSopMap.zone_id == zone,
                HazardSopMap.sop_step_id == sop_step_id)).first()
            if dup is not None:
                errs.append("이미 같은 연결이 있습니다.")

        if errs:
            db.rollback()
            return _page(request, db, user, errors=errs, status_code=400)

        db.add(HazardSopMap(hazard_type_code=code, level_code=level,
                            zone_id=zone, sop_step_id=sop_step_id,
                            seq=seq or 0))
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="위험유형별 SOP 연결 추가",
                     after={"hazard": code, "level": level or SOP.ANY_LABEL,
                            "zone": zone or SOP.ANY_LABEL,
                            "step": step.title if step else ""})
        db.commit()
        return _page(request, db, user, notice="연결을 추가했습니다.")

    @app.post("/settings/hazard-sop/delete", response_class=HTMLResponse)
    def hazard_sop_delete(request: Request, row_id: int = Form(0),
                          db: Session = Depends(get_db),
                          user: User = Depends(require_page(R.SOP,
                                                            R.Action.EDIT))):
        row = db.get(HazardSopMap, row_id) if row_id else None
        if row is None:
            return _page(request, db, user,
                         errors=["지울 연결을 찾을 수 없습니다."],
                         status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="위험유형별 SOP 연결 삭제",
                     before={"hazard": row.hazard_type_code,
                             "level": row.level_code or SOP.ANY_LABEL,
                             "zone": row.zone_id or SOP.ANY_LABEL})
        db.delete(row)
        db.commit()
        return _page(request, db, user, notice="연결을 지웠습니다.")
