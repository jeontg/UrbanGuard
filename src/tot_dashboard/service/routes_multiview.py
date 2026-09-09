"""S-05 멀티뷰 영상 관제 · S-07 탐지 피드백.

세 제안요청서가 요구하는데 우리에게 없던 두 화면이다
(`docs/202608181305/menu_structure_plan.md` 5절).

- **S-05** 경남 SFR-001 「화면 분할·배치를 자유롭게 구성하고 **저장**」,
  SFR-003 「이벤트 발생 시 해당 채널을 **시각적으로 강조**」
- **S-07** 경남 SFR-003 「조치 결과를 입력하여 **학습 데이터로 활용**」,
  서울 SFR-009·SFR-015
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core import events as EV
from ..core import feedback as FB
from ..core import multiview as MV
from ..core import roles as R
from ..core import user_prefs as UP
from ..core import vocabulary as V
from ..core.auth import client_ip, get_db, require_page
from ..core.models import Camera, Event, User

log = logging.getLogger("urbanguard.multiview")


def _scope(user: User) -> set[str] | None:
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def register(app, templates, base_ctx) -> None:

    # ---------- S-05 멀티뷰 ----------
    def _mv_page(request: Request, db: Session, user: User, *,
                 layout=None, notice: str = "", errors: list[str] | None = None,
                 status_code: int = 200):
        layouts = MV.layouts_of(db, user.id)
        cur = layout or MV.default_layout(db, user.id)
        tiles = cur.tiles if cur else MV.DEFAULT_TILES
        cams = MV.normalize(list(cur.cameras or []) if cur else [], tiles)
        return templates.TemplateResponse(request, "multiview.html", {
            **base_ctx(request, user), "active": "multiview",
            "all_cameras": list(db.scalars(select(Camera).order_by(Camera.id))),
            "layouts": layouts, "current": cur,
            "tiles": tiles, "slots": cams,
            "tile_choices": MV.TILE_CHOICES,
            "refresh_sec": int(MV.CACHE_TTL_SEC) + 1,
            # ★ 켜짐·꺼짐은 **사람에 붙는다.** 관제요원은 자리를 옮겨 앉는데
            #   브라우저에 두면 옆자리 PC 에서 설정이 사라지고, 본인은 껐다고
            #   생각한 것이 켜져 있는 상태가 된다.
            "mv_running": UP.flag(db, user.id, UP.KEY_MULTIVIEW_RUNNING),
            # 2026-09-02 실사용 중 발견 — 타일 배지가 등급("경계"/"주의")만
            # 보여주고 어느 도메인(침수/교통위험/인파관리/노면관리) 이벤트인지
            # 안 보였다. `/api/multiview/alerts`는 이미 도메인을 내려주고
            # 있었으니(코드 확인) 화면에서만 안 쓰고 있던 것 — 짧은 이름을
            # 넘겨 배지에 함께 찍는다(카메라 하나가 여러 도메인에 쓰일 수
            # 있어 등급만으로는 어느 도메인 때문인지 구분이 안 됨).
            "domain_short": R.DOMAIN_SHORT,
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/multiview", response_class=HTMLResponse)
    def multiview_page(request: Request, layout_id: int = 0,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.MONITOR,
                                                         R.Action.VIEW))):
        chosen = None
        if layout_id:
            got = db.get(MV.MultiviewLayout, layout_id)
            # ⚠️ 남의 배치는 못 본다. id 만 보고 열면 URL 을 바꿔 남의 배치를
            # 들여다볼 수 있다.
            if got is not None and got.user_id == user.id:
                chosen = got
        return _mv_page(request, db, user, layout=chosen)

    @app.post("/multiview/save", response_class=HTMLResponse)
    async def multiview_save(request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_page(R.MONITOR,
                                                               R.Action.VIEW))):
        form = await request.form()
        try:
            tiles = int((form.get("tiles") or MV.DEFAULT_TILES))
        except (TypeError, ValueError):
            tiles = -1
        cams = [(form.get(f"slot_{i}") or "").strip() for i in range(tiles)] \
            if tiles in MV.TILE_CHOICES else []
        row, errs = MV.save(db, user_id=user.id,
                            name=(form.get("name") or ""), tiles=tiles,
                            cameras=cams,
                            make_default=bool(form.get("make_default")))
        if errs:
            return _mv_page(request, db, user, errors=errs, status_code=400)
        db.commit()
        return _mv_page(request, db, user, layout=row, notice=(
            f"배치 「{row.name}」 을 저장했습니다."))

    @app.post("/multiview/delete", response_class=HTMLResponse)
    def multiview_delete(request: Request, layout_id: int = Form(0),
                         db: Session = Depends(get_db),
                         user: User = Depends(require_page(R.MONITOR,
                                                           R.Action.VIEW))):
        ok = MV.delete(db, user_id=user.id, layout_id=layout_id)
        db.commit()
        return _mv_page(request, db, user, notice=(
            "배치를 지웠습니다." if ok else "지울 배치를 찾을 수 없습니다."))

    @app.post("/api/multiview/running")
    def multiview_running(running: str = Form(""),
                          db: Session = Depends(get_db),
                          user: User = Depends(require_page(R.MONITOR,
                                                            R.Action.VIEW))):
        """멀티뷰 동작 켜기·끄기를 **이 사람 계정에** 저장한다.

        ⚠️ **감사 로그에 남기지 않는다.** 화면 설정이라 조치 이력이 아니고,
        관제 중 자주 눌리는 것이라 남기면 정작 봐야 할 기록이 묻힌다.

        ⚠️ **저장 실패를 조용히 넘기지 않는다.** 화면은 이미 멈춘 상태인데
        설정이 안 남으면, 다음에 열었을 때 **본인이 끈 줄 알았던 것이 켜져
        있다.** 그래서 저장된 값을 그대로 돌려주고 화면이 대조하게 한다.
        """
        on = str(running).strip() not in ("0", "false", "off")
        UP.set_flag(db, user.id, UP.KEY_MULTIVIEW_RUNNING, on)
        db.commit()
        return JSONResponse({
            "running": UP.flag(db, user.id, UP.KEY_MULTIVIEW_RUNNING)})

    @app.get("/api/multiview/tile/{camera_id}")
    def multiview_tile(camera_id: str, db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.MONITOR,
                                                         R.Action.VIEW))):
        """타일 한 장. 화면이 주기적으로 부른다.

        ⚠️ **마스킹 상태를 함께 준다.** 가리지 못한 화면을 조용히 띄우면
        여러 명이 보는 관제실에서 개인정보가 그대로 노출된다.
        """
        b64, mask_state, err = MV.frame_of(db, camera_id)
        return JSONResponse({"camera_id": camera_id, "image": b64,
                             "mask": mask_state, "error": err})

    @app.get("/api/multiview/alerts")
    def multiview_alerts(db: Session = Depends(get_db),
                         user: User = Depends(require_page(R.MONITOR,
                                                           R.Action.VIEW))):
        """열려 있는 이벤트를 지점별로. 타일 강조에 쓴다 (경남 SFR-003).

        같은 지점에 여러 건이 열려 있으면 **가장 높은 등급**을 준다 —
        관제요원이 봐야 하는 것은 「가장 급한 것」이다.

        ⚠️ 2026-09-02 — `!= "closed"` 대신 `EV.ACTIVE`(open+progress)
        화이트리스트를 쓴다. S-01/S-02는 이미 `EV.ACTIVE`로 "지금 봐야
        할 것"을 정의하고 있는데, 여기만 블랙리스트(닫힌 것만 제외)를
        쓰면 새 상태(예: 자동 보류, 재탐지 없이 오래 방치된 이벤트)가
        생겼을 때 이 화면만 계속 "활성"으로 착각해 카메라를 계속
        강조하게 된다 — "지금 봐야 할 상황을 시각적으로 강조한다"는
        멀티뷰의 목적과 맞지 않는다.
        """
        scope = _scope(user)
        q = select(Event).where(Event.status.in_(EV.ACTIVE))
        if scope is not None:
            q = q.where(Event.domain.in_(list(scope) or [""]))
        out: dict[str, dict] = {}
        for ev in db.scalars(q):
            if not ev.block_id:
                continue
            rank = V.rank(db, ev.level)
            got = out.get(ev.block_id)
            if got is None or rank > got["rank"]:
                out[ev.block_id] = {"event_id": ev.id, "level": ev.level,
                                    "rank": rank, "domain": ev.domain}
        return JSONResponse({"alerts": out})

    # ---------- S-07 탐지 피드백 ----------
    def _fb_page(request: Request, db: Session, user: User, *,
                 notice: str = "", errors: list[str] | None = None,
                 status_code: int = 200):
        scope = _scope(user)
        return templates.TemplateResponse(request, "feedback.html", {
            **base_ctx(request, user), "active": "feedback",
            "pending": FB.pending_events(db, allowed_domains=scope),
            "recent": FB.recent(db, limit=30),
            "counts": FB.counts(db),
            "labels": FB.VERDICT_LABELS,
            "event_verdicts": FB.EVENT_VERDICTS,
            "cameras": list(db.scalars(select(Camera).order_by(Camera.id))),
            "hazards": V.detectable_types(db),
            "domain_labels": R.DOMAIN_LABELS,
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_page(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.EVENT,
                                                        R.Action.VIEW))):
        return _fb_page(request, db, user)

    @app.post("/feedback/judge", response_class=HTMLResponse)
    def feedback_judge(request: Request, event_id: int = Form(0),
                       verdict: str = Form(""), reason: str = Form(""),
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.EVENT,
                                                         R.Action.EDIT))):
        ev = db.get(Event, event_id)
        scope = _scope(user)
        if ev is None or (scope is not None and ev.domain not in scope):
            return _fb_page(request, db, user, status_code=404,
                            errors=["이벤트를 찾을 수 없거나 권한이 없습니다."])
        row, errs = FB.record(db, verdict=verdict, user=user, event=ev,
                              reason=reason)
        if errs:
            return _fb_page(request, db, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request),
                     target=f"탐지 판정 #{ev.id} {FB.VERDICT_LABELS[verdict]}")
        db.commit()
        return _fb_page(request, db, user, notice=(
            f"#{ev.id} 를 「{FB.VERDICT_LABELS[verdict]}」 으로 판정했습니다. "
            "⚠ 판정했다고 곧바로 학습되지는 않습니다 — 라벨 확인 단계가 "
            "남아 있습니다."))

    @app.post("/feedback/missed", response_class=HTMLResponse)
    def feedback_missed(request: Request, camera_id: str = Form(""),
                        domain: str = Form(""),
                        hazard_type_code: str = Form(""),
                        occurred_at: str = Form(""), reason: str = Form(""),
                        db: Session = Depends(get_db),
                        user: User = Depends(require_page(R.EVENT,
                                                          R.Action.EDIT))):
        """미탐 신고 — 있었는데 탐지가 못 잡은 건.

        ⚠️ 붙일 이벤트가 없다. 그래서 지점·시각·유형을 사람이 적는다.
        """
        when = None
        raw = (occurred_at or "").strip()
        if raw:
            try:
                # 브라우저 datetime-local 은 초가 없을 수 있다.
                when = datetime.fromisoformat(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except ValueError:
                return _fb_page(request, db, user, status_code=400,
                                errors=["발생 시각 형식이 올바르지 않습니다."])
        row, errs = FB.record(db, verdict=FB.VERDICT_MISSED, user=user,
                              camera_id=camera_id, domain=domain,
                              hazard_type_code=hazard_type_code,
                              reason=reason, occurred_at=when)
        if errs:
            return _fb_page(request, db, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"미탐 신고 {camera_id}")
        db.commit()
        return _fb_page(request, db, user, notice=(
            "미탐을 신고했습니다. 학습 데이터 후보로 쌓입니다."))
