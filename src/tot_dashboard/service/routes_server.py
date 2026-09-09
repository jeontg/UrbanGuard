"""S-87 서버 운영 — 운영체제와 재기동 방식 — 시스템관리자 전용.

이 시스템은 **리눅스와 윈도우 양쪽에 납품된다.** 여기서 어느 환경인지와
누가 프로세스를 지켜보는지를 정하면, S-01 의 재기동 버튼이 그에 맞게 동작하고
안내문·로그 위치도 그 환경의 것으로 바뀐다.

화면이 **설정값과 실제 감지값을 나란히** 보여 준다. 하나만 보여 주면 설정이
현실과 어긋나 있어도 화면은 멀쩡해 보인다(:mod:`..core.server_profile`).
"""
from __future__ import annotations

import os

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..core import audit, settings
from ..core import roles as R
from ..core import server_profile as SP
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User
from .routes_services import SERVICES


def current_profile(db: Session | None = None) -> dict:
    """설정을 읽어 현황을 만든다. 다른 화면(S-01)도 이걸 쓴다."""
    return SP.profile(
        os_choice=settings.get(settings.KEY_SERVER_OS, db),
        strategy=settings.get(settings.KEY_RESTART_STRATEGY, db),
        service_name=settings.get(settings.KEY_SERVICE_NAME, db),
        ack=settings.get(settings.KEY_SUPERVISION_ACK, db) == "1")


def _performance_ctx(db: Session | None) -> dict:
    """"성능" 카드가 쓰는 값을 한곳에 모은다 — 화면 렌더링과 저장 후
    "합계 초과" 경고 계산이 똑같은 계산을 두 번 베끼지 않게 한다.

    ⚠️ 2026-09-02 사용자 결정 — 서비스별로 **다른** 스레드 상한을 지정할
    수 있게 하고, 코어 수로 막지 않는 대신 "지금 설정대로 5개 서비스가
    전부 뜨면 코어 수를 넘는가"를 항상 계산해 관리자가 인지하게 한다."""
    cpu_count = os.cpu_count()
    recommended = max(1, (cpu_count or 4) // 4)
    overrides = settings.service_thread_overrides(db)
    service_list = [(key, label) for key, label, _p, _s in SERVICES]
    # 서비스마다 "실제로 적용될 값"(관리자가 정한 값, 없으면 자동값) —
    # 5개를 전부 더한 게 이 서버가 동시에 뜰 때 실제로 쓰려 드는 총
    # 스레드 수다.
    effective = {key: overrides.get(key, recommended) for key, _l in service_list}
    total_effective = sum(effective.values())
    return {
        "cpu_count": cpu_count,
        "recommended_torch_threads": recommended,
        "service_list": service_list,
        "cur_service_threads": overrides,
        "effective_service_threads": effective,
        "total_effective_threads": total_effective,
        "threads_over_limit": bool(cpu_count) and total_effective > cpu_count,
    }


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", error: str = "", status_code: int = 200):
        prof = current_profile(db)
        # 화면에서 고를 수 있는 방식은 **선택한 OS 기준**이다. 리눅스에
        # 윈도우 서비스를 고르는 조합을 아예 못 만들게 한다.
        os_choice = settings.get(settings.KEY_SERVER_OS, db)
        pick_os = prof["os"]
        return templates.TemplateResponse(request, "settings_server.html", {
            **base_ctx(request, user), "active": "set-server",
            "prof": prof,
            "os_choice": os_choice,
            "os_options": [(SP.OS_AUTO, SP.OS_LABELS[SP.OS_AUTO]),
                           (SP.OS_LINUX, SP.OS_LABELS[SP.OS_LINUX]),
                           (SP.OS_WINDOWS, SP.OS_LABELS[SP.OS_WINDOWS])],
            # 방식 목록은 OS 별로 다르므로 둘 다 넘겨 화면에서 즉시 바꾼다.
            "strategies_by_os": {
                o: [(s, SP.STRATEGY_LABELS[s]) for s in ss]
                for o, ss in SP.STRATEGIES_BY_OS.items()},
            "cur_strategy": settings.get(settings.KEY_RESTART_STRATEGY, db),
            "cur_name": settings.get(settings.KEY_SERVICE_NAME, db),
            "cur_ack": settings.get(settings.KEY_SUPERVISION_ACK, db) == "1",
            "pick_os": pick_os,
            "strategy_labels": SP.STRATEGY_LABELS,
            # 화면 스크립트가 쓰는 목록. 코드와 화면이 어긋나지 않게 넘겨 준다.
            "needs_ack": list(SP.NEEDS_ACK),
            "no_service_name": list(SP.NO_SERVICE_NAME),
            "guides": {s: SP.guide_for(s, settings.get(
                settings.KEY_SERVICE_NAME, db)) for s in SP.STRATEGY_LABELS},
            # 2026-09-02 신설(속도 개선 2단계, 이후 서비스별 값으로
            # 재설계) — 서비스별 스레드 상한. `os.cpu_count()`는 이
            # 화면(웹 프로세스)에서 독립적으로 다시 불렀다 — 값을 만드는
            # `scripts/serve.py`는 별개 프로세스라 재사용할 수 없지만,
            # 같은 서버이므로 값은 어차피 같다.
            **_performance_ctx(db),
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/settings/server", response_class=HTMLResponse)
    def server_settings(request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_page(R.SETTINGS_SYS,
                                                          R.Action.VIEW))):
        return _page(request, db, user)

    @app.post("/settings/server", response_class=HTMLResponse)
    def server_settings_save(request: Request, server_os: str = Form("auto"),
                             restart_strategy: str = Form("none"),
                             service_name: str = Form(""),
                             supervision_ack: str = Form("0"),
                             db: Session = Depends(get_db),
                             user: User = Depends(require_page(
                                 R.SETTINGS_SYS, R.Action.EDIT))):
        if server_os not in SP.OS_LABELS:
            return _page(request, db, user, status_code=400,
                         error="운영체제 값이 올바르지 않습니다.")
        # 고른 OS 에서 쓸 수 없는 방식은 여기서 막는다. 화면이 막아도
        # 요청은 직접 보낼 수 있으므로 서버에서 한 번 더 본다.
        eff_os, eff_strategy = SP.normalize(server_os, restart_strategy)
        if eff_strategy != restart_strategy:
            return _page(request, db, user, status_code=400,
                         error=(f"「{SP.OS_LABELS[eff_os]}」에서는 "
                                f"그 재기동 방식을 쓸 수 없습니다."))

        before = {
            "os": settings.get(settings.KEY_SERVER_OS, db),
            "strategy": settings.get(settings.KEY_RESTART_STRATEGY, db),
            "name": settings.get(settings.KEY_SERVICE_NAME, db),
            "ack": settings.get(settings.KEY_SUPERVISION_ACK, db),
        }
        settings.set_value(db, settings.KEY_SERVER_OS, server_os)
        settings.set_value(db, settings.KEY_RESTART_STRATEGY, restart_strategy)
        settings.set_value(db, settings.KEY_SERVICE_NAME,
                           service_name.strip()[:64])
        # 「감시 확인」은 **폼에 적힌 대로** 저장한다. 관리자가 지금 보고 있는
        # 폼에 표시한 것이므로, 같은 제출에서 방식을 바꿨더라도 새 방식에 대한
        # 확인이 맞다.
        #
        # 확인이 필요 없는 방식(systemd 등)이면 값이 와도 0으로 눕힌다 —
        # 화면에서는 체크박스가 아예 안 뜨므로, 값이 왔다면 직접 만든 요청이다.
        ack = "1" if (supervision_ack == "1"
                      and SP.needs_ack(restart_strategy)) else "0"
        settings.set_value(db, settings.KEY_SUPERVISION_ACK, ack)
        db.commit()
        settings.load_all(db)

        audit.record_and_commit(
            db, action=audit.SETTINGS_UPDATE, user=user, ip=client_ip(request),
            target="서버 운영(S-87)", before=before,
            after={"os": server_os, "strategy": restart_strategy,
                   "name": service_name.strip()[:64], "ack": ack})

        msg = "서버 운영 설정을 저장했습니다."
        if supervision_ack == "1" and ack == "0":
            msg += (f" 「{SP.STRATEGY_LABELS[restart_strategy]}」는 감시 여부를 "
                    "프로그램이 확인할 수 있어 「감시 확인」 표시는 쓰지 않습니다.")
        return _page(request, db, user, notice=msg)

    # 2026-09-02 신설(속도 개선 2단계, 이후 서비스별 값으로 재설계) —
    # 서비스별 스레드 상한. 위 OS/재기동 방식 폼과 관심사가 달라 별도
    # 폼·라우트로 뺐다(하나가 저장 실패해도 다른 하나에 영향이 없게).
    @app.post("/settings/server/performance", response_class=HTMLResponse)
    async def server_performance_save(request: Request, db: Session = Depends(get_db),
                                      user: User = Depends(require_page(
                                          R.SETTINGS_SYS, R.Action.EDIT))):
        # ⚠️ 2026-09-02 — 코어 수 상한을 여기서 **막지 않는다**(사용자
        # 결정). PyTorch/OMP는 코어 수보다 큰 값을 줘도 기술적으로
        # 오류가 나지 않는다 — 그저 이득 없이 스케줄링 경합만 늘 뿐이다.
        # 그래서 "막는" 대신 관리자가 **인지**하게 한다 — 5개 서비스별
        # 값을 전부 더한 합계와 이 서버의 코어 수를 나란히 보여주고,
        # 합계가 코어 수를 넘으면 "초과" 표시를 지속적으로 띄운다(이
        # 페이지 머리말의 기존 설계 원칙 "설정값과 실제 감지값을 나란히
        # 보여 준다"를 그대로 적용). 정말로 못 쓰는 값(0 이하·숫자
        # 아님)만 서비스별로 거부한다.
        #
        # 서비스마다 필드 이름이 달라 개별 Form() 대신 원본 폼 데이터를
        # 직접 읽는다 — SERVICES 목록이 나중에 늘어나도 이 라우트를
        # 다시 안 고쳐도 되게.
        form = await request.form()
        overrides: dict[str, int] = {}
        for key, label, _p, _s in SERVICES:
            raw = str(form.get(f"threads_{key}", "")).strip()
            if not raw:
                continue
            try:
                n = int(raw)
            except ValueError:
                n = None
            if n is None or n < 1:
                return _page(request, db, user, status_code=400,
                             error=f"「{label}」 값이 올바르지 않습니다 — "
                                   "1 이상의 정수를 입력하거나 비워 두십시오.")
            overrides[key] = n

        before = settings.get(settings.KEY_SERVICE_THREADS, db)
        saved = settings.set_service_thread_overrides(db, overrides)
        db.commit()

        audit.record_and_commit(
            db, action=audit.SETTINGS_UPDATE, user=user, ip=client_ip(request),
            target="서버 성능(S-87)", before={"service_threads": before},
            after={"service_threads": saved})

        msg = "저장했습니다. 다음 서비스 재기동부터 적용됩니다."
        ctx = _performance_ctx(db)
        if ctx["threads_over_limit"]:
            msg += (f" ⚠ 5개 서비스 스레드 상한을 모두 더하면 "
                    f"{ctx['total_effective_threads']}개인데, 이 서버의 코어 "
                    f"수는 {ctx['cpu_count']}개입니다 — 저장은 되지만 실제 "
                    "이득 없이 스케줄링 경합만 늘어날 수 있습니다.")
        return _page(request, db, user, notice=msg)
