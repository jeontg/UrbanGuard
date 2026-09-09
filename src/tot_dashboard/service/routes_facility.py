"""S-11 시설물 원격제어.

설계서 4절. 화면에 반드시 지켜야 할 세 가지:
  1. 현재 모드를 항상 상단에 크게 — 자동인지 아닌지 모르는 상태가 가장 위험
  2. 모든 제어 시도는 성공·실패 무관하게 기록
  3. 연동이 끊긴 시설은 「미연계」로 명확히 — 연동된 줄 알고 방치하는 것이 최악
"""
from __future__ import annotations

import logging

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..core import audit, facilities as F
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

log = logging.getLogger("urbanguard.facility")


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *, notice: str = "",
              error: str = "", status_code: int = 200):
        mode = F.current_mode(db)
        return templates.TemplateResponse(request, "facility.html", {
            **base_ctx(request, user), "active": "facility",
            "facilities": F.load(), "mode": mode,
            "mode_labels": F.MODE_LABELS, "mode_desc": F.MODE_DESC,
            "modes": [(m, F.MODE_LABELS[m], F.MODE_DESC[m]) for m in F.MODES],
            "allowed": F.allowed_commands(mode),
            "link_labels": F.LINK_LABELS, "type_labels": F.TYPE_LABELS,
            "cmd_labels": F.CMD_LABELS, "result_labels": F.RESULT_LABELS,
            "history": F.history(db, limit=30),
            "can_advise": R.can(user.role, R.FACILITY, R.Action.REQUEST),
            "can_remote": R.can(user.role, R.FACILITY, R.Action.EXECUTE),
            "can_mode": R.can(user.role, R.FACILITY_MODE, R.Action.EDIT),
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/facility", response_class=HTMLResponse)
    def facility_page(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.FACILITY,
                                                        R.Action.VIEW))):
        return _page(request, db, user)

    @app.post("/facility/mode", response_class=HTMLResponse)
    def facility_mode(request: Request, mode: str = Form(...),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.FACILITY_MODE,
                                                        R.Action.EDIT))):
        try:
            before = F.set_mode(db, mode)
        except ValueError as e:
            return _page(request, db, user, error=str(e), status_code=400)
        # 모드 변경은 시스템의 책임 범위를 바꾸는 행위다. 반드시 남긴다.
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="시설물 제어 모드 변경",
                     before={"mode": before}, after={"mode": mode})
        db.commit()
        msg = f"제어 모드를 「{F.MODE_LABELS[mode]}」로 바꿨습니다."
        if mode == F.MODE_AUTO:
            msg += " ⚠ 자동 제어는 별도 계약·보험이 전제되어야 합니다."
        return _page(request, db, user, notice=msg)

    @app.post("/facility/{facility_id}/command", response_class=HTMLResponse)
    def facility_command(facility_id: str, request: Request,
                         command: str = Form(...), memo: str = Form(""),
                         db: Session = Depends(get_db),
                         user: User = Depends(require_page(R.FACILITY,
                                                           R.Action.VIEW))):
        fac = F.get(facility_id)
        if fac is None:
            return _page(request, db, user, error="시설을 찾을 수 없습니다.",
                         status_code=404)

        need = {F.CMD_STATUS: R.Action.VIEW, F.CMD_ADVISE: R.Action.REQUEST,
                F.CMD_REMOTE: R.Action.EXECUTE}.get(command)
        if need is None:
            return _page(request, db, user, error="알 수 없는 명령입니다.",
                         status_code=400)
        if not R.can(user.role, R.FACILITY, need):
            return _page(request, db, user, status_code=403,
                         error=f"「{F.CMD_LABELS[command]}」 권한이 없습니다.")

        mode = F.current_mode(db)
        row = F.execute(db, facility=fac, command=command, user=user,
                        mode=mode, memo=memo.strip())
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request),
                     target=f"시설 제어 {fac.get('name')} / {F.CMD_LABELS[command]}",
                     after={"result": row.result, "mode": mode})
        db.commit()

        if row.result == F.RESULT_NOT_LINKED:
            msg = (f"{fac.get('name')} — 기록만 남았습니다. "
                   "연동 규격이 확보되지 않아 실제 명령은 전달되지 않았습니다.")
        elif row.result == F.RESULT_BLOCKED:
            msg = (f"현재 모드(「{F.MODE_LABELS[mode]}」)에서는 이 명령을 보낼 수 "
                   "없습니다. 시스템관리자에게 모드 변경을 요청하세요.")
            return _page(request, db, user, error=msg, status_code=403)
        else:
            msg = f"{fac.get('name')} — {F.CMD_LABELS[command]} 완료."
        return _page(request, db, user, notice=msg)
