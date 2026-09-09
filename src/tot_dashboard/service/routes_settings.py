"""S-85 기관 정보·화면 설정 — 시스템관리자 전용.

기관명·솔루션명·로고·상황판 배경색을 운영 중에 바꾼다. 하드코딩된 "부산광역시"와
"통합 도시안전 관제", UrbanGuard 로고가 전부 여기서 갈린다(제품화 계획 P0-5).
"""
from __future__ import annotations

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..core import audit, branding, settings
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

# 로고 폭이 이보다 작으면 사이드바(고해상도 2배수 기준)에서 흐려진다.
MIN_LOGO_WIDTH = 200


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", error: str = "", bg: str | None = None,
              org: str | None = None, solution: str | None = None,
              status_code: int = 200):
        cur_bg = bg if bg is not None else settings.get(settings.KEY_BOARD_BG, db)
        cur_org = org if org is not None else settings.get(settings.KEY_ORG_NAME, db)
        cur_sol = (solution if solution is not None
                   else settings.get(settings.KEY_SOLUTION_NAME, db))
        logo_file = settings.get(settings.KEY_LOGO_FILE, db)
        size = branding.image_size(logo_file)
        return templates.TemplateResponse(request, "settings_org.html", {
            **base_ctx(request, user), "active": "set-org",
            "presets": settings.PRESETS,
            "cur_bg": cur_bg, "cur_org": cur_org, "cur_solution": cur_sol,
            "preview": settings.derive_theme(cur_bg),
            "warning": settings.contrast_warning(cur_bg),
            # 로고 정보 — 화면에 (000px, 000px) 로 표기한다.
            "logo_is_custom": bool(logo_file),
            "logo_size_text": branding.format_size(size),
            "logo_width": size[0] if size else 0,
            "logo_narrow": bool(size and size[0] < MIN_LOGO_WIDTH),
            "logo_recommended": branding.format_size(branding.RECOMMENDED),
            "max_logo_mb": branding.MAX_BYTES // 1024 // 1024,
            # S-01 배경지도. 비어 있으면 상황판은 배치 도식을 쓴다.
            "map_tile_url": settings.get(settings.KEY_MAP_TILE_URL, db),
            "map_attribution": settings.get(settings.KEY_MAP_ATTRIBUTION, db),
            "map_max_zoom": settings.get(settings.KEY_MAP_MAX_ZOOM, db),
            # 화면 정렬. 값이 바뀔 때마다 카드가 자리를 옮기는 것을 멈추는 설정.
            "card_orders": settings.CARD_ORDERS,
            "object_orders": settings.OBJECT_ORDERS,
            "card_order": settings.get(settings.KEY_CARD_ORDER, db),
            "object_order": settings.get(settings.KEY_OBJECT_ORDER, db),
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/settings/org", response_class=HTMLResponse)
    def org_settings(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.SETTINGS_SYS,
                                                       R.Action.VIEW))):
        return _page(request, db, user)

    @app.post("/settings/org", response_class=HTMLResponse)
    async def org_settings_save(request: Request, org_name: str = Form(...),
                                solution_name: str = Form(...),
                                board_bg: str = Form(...),
                                map_tile_url: str = Form(""),
                                map_attribution: str = Form(""),
                                map_max_zoom: str = Form("18"),
                                card_order: str = Form("severity"),
                                object_order: str = Form("drop"),
                                logo: UploadFile | None = File(None),
                                db: Session = Depends(get_db),
                                user: User = Depends(require_page(R.SETTINGS_SYS,
                                                                  R.Action.EDIT))):
        org_name = org_name.strip()
        solution_name = solution_name.strip()
        board_bg = board_bg.strip().upper()
        if not board_bg.startswith("#"):
            board_bg = "#" + board_bg

        def fail(msg: str):
            return _page(request, db, user, error=msg, bg=board_bg,
                         org=org_name, solution=solution_name, status_code=400)

        if not org_name:
            return fail("기관명을 입력하세요.")
        if not solution_name:
            return fail("솔루션명을 입력하세요.")
        if settings.parse_hex(board_bg) is None:
            return fail("색상 형식이 올바르지 않습니다. #RRGGBB 로 입력하세요.")

        # 배경지도. **비우면 끄는 것**이라 빈 값은 통과시킨다.
        map_tile_url = map_tile_url.strip()
        map_attribution = map_attribution.strip()
        if map_tile_url:
            if not map_tile_url.startswith(("http://", "https://")):
                return fail("타일 주소는 http:// 또는 https:// 로 시작해야 합니다.")
            missing = [t for t in ("{z}", "{x}", "{y}") if t not in map_tile_url]
            if missing:
                return fail("타일 주소에 " + " · ".join(missing) + " 가 없습니다. "
                            "예) https://tile.example.org/{z}/{x}/{y}.png")
        try:
            zoom = int((map_max_zoom or "18").strip())
        except ValueError:
            return fail("최대 확대 단계는 숫자여야 합니다.")
        if not (5 <= zoom <= 22):
            return fail("최대 확대 단계는 5~22 사이여야 합니다.")

        before = {
            "card_order": settings.get(settings.KEY_CARD_ORDER, db),
            "object_order": settings.get(settings.KEY_OBJECT_ORDER, db),
            "org_name": settings.get(settings.KEY_ORG_NAME, db),
            "solution_name": settings.get(settings.KEY_SOLUTION_NAME, db),
            "board_bg": settings.get(settings.KEY_BOARD_BG, db),
            "logo_file": settings.get(settings.KEY_LOGO_FILE, db),
            "map_tile_url": settings.get(settings.KEY_MAP_TILE_URL, db),
        }

        # 로고는 파일을 골랐을 때만 건드린다 — 다른 항목만 고치려던 사람이
        # 로고를 잃지 않도록.
        logo_note = ""
        if logo is not None and logo.filename:
            data = await logo.read()
            name, err = branding.save_logo(data, logo.filename)
            if err:
                return fail(err)
            # 이전 파일은 save_logo 가 이미 지웠다(한 번에 한 장만 둔다).
            settings.set_value(db, settings.KEY_LOGO_FILE, name)
            size = branding.image_size(name)
            logo_note = f" 로고를 교체했습니다 {branding.format_size(size)}."
            if size and size[0] < MIN_LOGO_WIDTH:
                logo_note += (f" 다만 가로 {size[0]}px 은 권장({MIN_LOGO_WIDTH}px 이상)보다"
                              " 작아 화면에서 흐리게 보일 수 있습니다.")

        settings.set_value(db, settings.KEY_ORG_NAME, org_name)
        settings.set_value(db, settings.KEY_SOLUTION_NAME, solution_name)
        settings.set_value(db, settings.KEY_BOARD_BG, board_bg)
        settings.set_value(db, settings.KEY_MAP_TILE_URL, map_tile_url)
        settings.set_value(db, settings.KEY_MAP_ATTRIBUTION, map_attribution)
        settings.set_value(db, settings.KEY_MAP_MAX_ZOOM, str(zoom))
        # 모르는 값이 오면 기본값으로 눕힌다 — 화면은 목록에서만 고르게 하므로,
        # 다른 값이 왔다면 직접 만든 요청이다.
        settings.set_value(
            db, settings.KEY_CARD_ORDER,
            card_order if card_order in settings.CARD_ORDERS else "severity")
        settings.set_value(
            db, settings.KEY_OBJECT_ORDER,
            object_order if object_order in settings.OBJECT_ORDERS else "drop")
        after = {
            "org_name": org_name, "solution_name": solution_name,
            "board_bg": board_bg,
            "logo_file": settings.get(settings.KEY_LOGO_FILE, db),
            "card_order": settings.get(settings.KEY_CARD_ORDER, db),
            "object_order": settings.get(settings.KEY_OBJECT_ORDER, db),
        }
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="기관 정보·화면 설정",
                     before=before, after=after)
        db.commit()

        # 대비가 기준에 못 미쳐도 저장은 막지 않는다 — 운영 판단이 우선이고,
        # 대신 경고를 계속 띄워 상태를 인지하게 한다(9-3절).
        warn = settings.contrast_warning(board_bg)
        msg = "저장했습니다." + logo_note
        if warn:
            msg += " 다만 접근성 경고가 있습니다 — 아래 안내를 확인하세요."
        return _page(request, db, user, notice=msg)

    @app.post("/settings/org/reset", response_class=HTMLResponse)
    def org_settings_reset(request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_page(R.SETTINGS_SYS,
                                                             R.Action.EDIT))):
        before = settings.get(settings.KEY_BOARD_BG, db)
        settings.set_value(db, settings.KEY_BOARD_BG,
                           settings.DEFAULTS[settings.KEY_BOARD_BG])
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="상황판 배경색 초기화",
                     before={"board_bg": before},
                     after={"board_bg": settings.DEFAULTS[settings.KEY_BOARD_BG]})
        db.commit()
        return _page(request, db, user, notice="배경색을 기본값으로 되돌렸습니다.")

    @app.post("/settings/org/logo/reset", response_class=HTMLResponse)
    def org_logo_reset(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_SYS,
                                                         R.Action.EDIT))):
        before = settings.get(settings.KEY_LOGO_FILE, db)
        if not before:
            return _page(request, db, user,
                         notice="이미 기본 로고를 쓰고 있습니다.")
        branding.clear_logo(before)
        settings.set_value(db, settings.KEY_LOGO_FILE, "")
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="기관 로고 초기화",
                     before={"logo_file": before}, after={"logo_file": ""})
        db.commit()
        return _page(request, db, user, notice="기본 로고로 되돌렸습니다.")
