"""S-02 이벤트 목록 · S-03 이벤트 상세·조치.

설계서 3절 「한 호흡 완결」 — 발견에서 통보·기록까지 이 화면에서 끝난다.
화면을 옮겨 다니면 골든타임을 깎아먹기 때문이다.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..core import audit, event_cause, events
from ..core import notifications as N
from ..core import relations as REL
from ..core import roles as R
from ..core import settings
from ..core import sop as SOP
from ..core.auth import client_ip, get_db, require_page
from sqlalchemy import select

from ..core.models import Event, EventEvidence, User

log = logging.getLogger("urbanguard.events")

LEVEL_BADGE = {"심각": "ug-badge--crit", "경계": "ug-badge--crit",
               "주의": "ug-badge--warn", "관심": "ug-badge--on"}
STATUS_BADGE = {events.OPEN: "ug-badge--crit", events.IN_PROGRESS: "ug-badge--warn",
                events.CLOSED: "ug-badge--off", events.AUTO_HELD: "ug-badge--off"}


def _scope(user: User) -> set[str] | None:
    """MGR 만 담당 도메인으로 제한된다. SYS·OPR 은 전 도메인."""
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def _auto_hold_ctx(db: Session) -> dict:
    """"자동 보류 정책" 카드가 쓰는 값 — 여러 렌더링 지점(목록·저장 후
    재렌더)이 같은 계산을 두 번 베끼지 않게 한다."""
    return {
        "auto_hold_enabled": settings.event_auto_hold_enabled(db),
        "auto_hold_hours": settings.event_auto_hold_hours(db),
    }


def register(app, templates, base_ctx) -> None:

    def _can(user: User, action: R.Action, domain: str | None = None) -> bool:
        return R.can(user.role, R.EVENT, action, domain=domain,
                     user_domains=user.domain_set)

    # ---------- S-02 이벤트 목록 ----------
    @app.get("/events", response_class=HTMLResponse)
    def event_list(request: Request, status: str = "active", domain: str = "",
                   db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.EVENT, R.Action.VIEW))):
        scope = _scope(user)
        rows = events.list_events(db, status=status, domain=domain,
                                  allowed_domains=scope)
        return templates.TemplateResponse(request, "events_list.html", {
            **base_ctx(request, user), "active": "events",
            "rows": rows, "counts": events.counts(db, scope),
            "sel_status": status, "sel_domain": domain,
            "status_labels": events.STATUS_LABELS,
            "level_badge": LEVEL_BADGE, "status_badge": STATUS_BADGE,
            "domain_labels": R.DOMAIN_LABELS,
            "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
            "elapsed": events.elapsed_text,
            # 정책 카드는 종결(APPROVE)과 같은 문턱 — 관제요원(EVENT/EDIT)은
            # 일상 조치는 하되 정책은 못 바꾼다(종결 권한과 동일 원칙).
            "can_edit_auto_hold": _can(user, R.Action.APPROVE),
            **_auto_hold_ctx(db),
        })

    # 2026-09-02 신설(이벤트 자동 보류 정책) — 사용자 결정: 관리자가
    # 이 화면(S-02)에서 직접 값을 바꿀 수 있어야 한다. 정책과 그 효과를
    # 같은 화면에서 보게 한다(오늘 만든 "서버 성능" 설정이 서버 운영
    # 화면에 있는 것과 같은 원칙).
    @app.post("/events/auto-hold-settings", response_class=HTMLResponse)
    def event_auto_hold_settings(request: Request, enabled: str = Form("0"),
                                 hours: str = Form("24"),
                                 db: Session = Depends(get_db),
                                 user: User = Depends(require_page(
                                     R.EVENT, R.Action.APPROVE))):
        try:
            n = float(hours)
        except ValueError:
            n = None
        if n is None or n < 1:
            scope = _scope(user)
            return templates.TemplateResponse(request, "events_list.html", {
                **base_ctx(request, user), "active": "events",
                "rows": events.list_events(db, status="active",
                                           allowed_domains=scope),
                "counts": events.counts(db, scope),
                "sel_status": "active", "sel_domain": "",
                "status_labels": events.STATUS_LABELS,
                "level_badge": LEVEL_BADGE, "status_badge": STATUS_BADGE,
                "domain_labels": R.DOMAIN_LABELS,
                "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
                "elapsed": events.elapsed_text,
                "can_edit_auto_hold": True,
                **_auto_hold_ctx(db),
                "error": "1 이상의 정수를 입력하십시오.",
            }, status_code=400)

        on = str(enabled).strip() == "1"
        before = _auto_hold_ctx(db)
        settings.set_event_auto_hold_enabled(db, on)
        settings.set_event_auto_hold_hours(db, hours)
        db.commit()

        audit.record_and_commit(
            db, action=audit.SETTINGS_UPDATE, user=user, ip=client_ip(request),
            target="이벤트 자동 보류 정책", before=before,
            after=_auto_hold_ctx(db))

        scope = _scope(user)
        return templates.TemplateResponse(request, "events_list.html", {
            **base_ctx(request, user), "active": "events",
            "rows": events.list_events(db, status="active", allowed_domains=scope),
            "counts": events.counts(db, scope),
            "sel_status": "active", "sel_domain": "",
            "status_labels": events.STATUS_LABELS,
            "level_badge": LEVEL_BADGE, "status_badge": STATUS_BADGE,
            "domain_labels": R.DOMAIN_LABELS,
            "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
            "elapsed": events.elapsed_text,
            "can_edit_auto_hold": True,
            **_auto_hold_ctx(db),
            "notice": "자동 보류 정책을 저장했습니다.",
        })

    # ---------- S-03 이벤트 상세 ----------
    def _detail(request: Request, db: Session, user: User, ev: Event, *,
                notice: str = "", error: str = "", status_code: int = 200):
        return templates.TemplateResponse(request, "event_detail.html", {
            **base_ctx(request, user), "active": "events", "ev": ev,
            "status_labels": events.STATUS_LABELS,
            "action_labels": events.ACTION_LABELS,
            "level_badge": LEVEL_BADGE, "status_badge": STATUS_BADGE,
            "domain_labels": R.DOMAIN_LABELS,
            "elapsed": events.elapsed_text(ev),
            # 2026-09-03 신설 — "이 이벤트가 왜 발생했는가"를 매 조회 시점에
            # 조합해 문장으로 만든다(core/event_cause.py 참고).
            "cause": event_cause.explain(db, ev),
            "can_close": _can(user, R.Action.APPROVE, ev.domain),
            "can_notify_agency": R.can(user.role, R.NOTIFY_AGENCY,
                                       R.Action.REQUEST, domain=ev.domain,
                                       user_domains=user.domain_set),
            "can_notify_dept": R.can(user.role, R.NOTIFY, R.Action.REQUEST,
                                     domain=ev.domain,
                                     user_domains=user.domain_set),
            # 디지털 SOP. 체크는 이벤트 처리 행위라 EVENT/EDIT 로 판정하고,
            # 단계 정의를 고칠 수 있는지는 따로 본다(SOP/EDIT).
            "sop": SOP.progress(db, ev),
            # 증거 영상(S-88). 조치한 사람이 자기 판단의 근거를 볼 수 있어야
            # 하고, 사후 검토도 여기서 시작된다.
            "evidence": db.scalars(
                select(EventEvidence)
                .where(EventEvidence.event_id == ev.id)
                .order_by(EventEvidence.kind, EventEvidence.id)).all(),
            "can_see_evidence": R.can(user.role, R.EVIDENCE, R.Action.VIEW),
            "can_check": _can(user, R.Action.EDIT, ev.domain)
                         and ev.status != events.CLOSED,
            "can_edit_sop": R.can(user.role, R.SOP, R.Action.EDIT),
            # 주변 지점 (관계 모델 1단계). 근거는 경남 제안요청서 SFR-001
            # 「연속 추적」과 통상 사양 「알람 발생 카메라 주변 다중 카메라
            # 자동 확인」. 관계가 없으면 빈 목록이라 화면이 그냥 안 보인다.
            "nearby": REL.nearby_cameras(db, ev.block_id) if ev.block_id else [],
            "notice": notice, "error": error,
        }, status_code=status_code)

    def _load(db: Session, user: User, event_id: int) -> Event | None:
        ev = db.get(Event, event_id)
        if ev is None:
            return None
        scope = _scope(user)
        if scope is not None and ev.domain not in scope:
            return None
        return ev

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_detail(event_id: int, request: Request,
                     db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.EVENT, R.Action.VIEW))):
        ev = _load(db, user, event_id)
        if ev is None:
            return templates.TemplateResponse(request, "events_list.html", {
                **base_ctx(request, user), "active": "events", "rows": [],
                "counts": events.counts(db, _scope(user)),
                "sel_status": "active", "sel_domain": "",
                "status_labels": events.STATUS_LABELS,
                "level_badge": LEVEL_BADGE, "status_badge": STATUS_BADGE,
                "domain_labels": R.DOMAIN_LABELS,
                "domains": [(d.value, R.DOMAIN_LABELS[d]) for d in R.Domain],
                "elapsed": events.elapsed_text,
                "can_edit_auto_hold": _can(user, R.Action.APPROVE),
                **_auto_hold_ctx(db),
                "error": "이벤트를 찾을 수 없거나 조회 권한이 없습니다.",
            }, status_code=404)
        return _detail(request, db, user, ev)

    @app.post("/events/{event_id}/action", response_class=HTMLResponse)
    def event_action(event_id: int, request: Request, act: str = Form(...),
                     memo: str = Form(""),
                     db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.EVENT, R.Action.EDIT))):
        ev = _load(db, user, event_id)
        if ev is None:
            return RedirectResponse(url="/events", status_code=303)

        memo = (memo or "").strip()
        ip = client_ip(request)
        dom = ev.domain

        def deny(msg: str):
            return _detail(request, db, user, ev, error=msg, status_code=403)

        if act == events.ACT_ACKNOWLEDGE:
            events.acknowledge(db, ev, user, memo)
            msg = "확인 처리했습니다."
        elif act == events.ACT_MEMO:
            if not memo:
                return _detail(request, db, user, ev,
                               error="조치 내용을 입력하세요.", status_code=400)
            events.add_action(db, ev, events.ACT_MEMO, user=user, memo=memo)
            msg = "조치를 기록했습니다."
        elif act in (events.ACT_NOTIFY_DEPT, "notify_resident"):
            tier = (N.TIER_RESIDENT if act == "notify_resident" else N.TIER_DEPT)
            if not R.can(user.role, R.NOTIFY, R.Action.REQUEST, domain=dom,
                         user_domains=user.domain_set):
                return deny("통보 요청 권한이 없습니다.")
            body = memo or (f"[{ev.place_name or ev.block_id}] "
                            f"{ev.event_type or ev.domain} 「{ev.level}」 상황입니다.")
            n = N.request(db, event_id=ev.id, domain=dom, tier=tier, body=body,
                          user=user, risk_level=ev.level)
            events.add_action(db, ev, events.ACT_NOTIFY_DEPT, user=user,
                              memo=f"{N.TIER_LABELS[tier]} 요청 (#{n.id})")
            audit.record(db, action=audit.NOTIFY_REQUEST, user=user, ip=ip,
                         target=f"이벤트 #{ev.id} {N.TIER_LABELS[tier]} 요청 #{n.id}")
            if N.can_self_approve(user, tier, ev.level, db=db):
                # 부서 통보는 되돌릴 수 있으므로 단독 진행한다.
                # 주민 경보가 여기로 오는 경우는 「심각」 단독 발송 설정뿐이며,
                # 그때는 역할과 무관하게 사후 승인 대상으로 남긴다.
                post = (tier == N.TIER_RESIDENT)
                N.approve(db, n, user, post_approval=post)
                msg = (f"{N.TIER_LABELS[tier]}를 요청·승인했습니다. "
                       "발송 이력에서 확인하세요.")
                if post:
                    msg += " ※ 「심각」 단독 발송이므로 사후 승인이 필요합니다."
                    audit.record(db, action=audit.NOTIFY_SOLO_SEND, user=user,
                                 ip=ip, target=f"알림 #{n.id}")
            else:
                msg = (f"{N.TIER_LABELS[tier]}를 요청했습니다. "
                       "부서담당자 승인 후 발송됩니다.")
        elif act == events.ACT_NOTIFY_AGENCY:
            if not R.can(user.role, R.NOTIFY_AGENCY, R.Action.REQUEST,
                         domain=dom, user_domains=user.domain_set):
                return deny("기관 통보 권한이 없습니다.")
            events.add_action(db, ev, events.ACT_NOTIFY_AGENCY, user=user, memo=memo)
            audit.record(db, action=audit.NOTIFY_SEND, user=user, ip=ip,
                         target=f"이벤트 #{ev.id} 기관 통보(112·119·재난상황실)")
            # ⚠️ 외부 연계 규격 미확보 — 지금은 「통보했음」을 기록만 한다(S-70).
            msg = "기관 통보를 기록했습니다. (외부 연계는 규격 확보 후 연동)"
        elif act == events.ACT_FALSE_POSITIVE:
            events.mark_false_positive(db, ev, user, memo)
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user, ip=ip,
                         target=f"이벤트 #{ev.id} 오탐 신고")
            msg = "오탐으로 신고하고 종결했습니다. 재학습 데이터로 수집됩니다."
        elif act == events.ACT_CLOSE:
            if not _can(user, R.Action.APPROVE, dom):
                return deny("이벤트 종결 권한이 없습니다. 부서담당자에게 요청하세요.")
            # SOP 이행 현황을 종결 메모에 박아 둔다. 종결하면 등급이 고정돼
            # 나중에 다시 계산할 수 없고, 사후 검토는 이력만 읽는다.
            state = SOP.summary_text(db, ev)
            events.close(db, ev, user, f"{memo} [{state}]".strip())
            # ⚠️ 2026-09-02 — 지금까지 사람의 종결이 `EventAction`(이벤트별
            # 이력)에는 남았지만 전역 감사 로그(S-61)에는 안 남던 공백을
            # 메운다(이벤트 자동 보류 정책 도입 중 발견). 자동 보류
            # 스레드도 같은 화면(S-61)에서 대칭적으로 추적돼야 한다.
            audit.record(db, action=audit.EVENT_CLOSE, user=user, ip=ip,
                         target=f"이벤트 #{ev.id} {ev.place_name or ev.block_id}")
            msg = "종결 처리했습니다."
            left = SOP.progress(db, ev)["required_left"]
            if left:
                msg += f" ※ 필수 SOP {left}건 미이행으로 기록됐습니다."
        else:
            return _detail(request, db, user, ev, error="알 수 없는 조치입니다.",
                           status_code=400)

        db.commit()
        log.info("이벤트 조치 id=%s act=%s by=%s", ev.id, act, user.login_id)
        return _detail(request, db, user, ev, notice=msg)

    # ---------- 디지털 SOP 이행 체크 (S-86 에서 정의한 단계) ----------
    #
    # 권한을 SOP 가 아니라 EVENT/EDIT 로 판정한다. SOP 를 따르는 것은 이벤트
    # 처리 행위이고, 그 권한은 관제요원에게 이미 있다. SOP/EDIT 로 잠그면
    # 정작 절차를 수행하는 사람이 체크를 못 한다.
    @app.post("/events/{event_id}/sop", response_class=HTMLResponse)
    def event_sop(event_id: int, request: Request, step_id: int = Form(...),
                  op: str = Form("check"), note: str = Form(""),
                  db: Session = Depends(get_db),
                  user: User = Depends(require_page(R.EVENT, R.Action.EDIT))):
        ev = _load(db, user, event_id)
        if ev is None:
            return RedirectResponse(url="/events", status_code=303)
        if ev.status == events.CLOSED:
            return _detail(request, db, user, ev, status_code=400,
                           error="종결된 이벤트는 체크할 수 없습니다.")

        note = (note or "").strip()
        if op == "uncheck":
            if not SOP.uncheck(db, event_id, step_id):
                return _detail(request, db, user, ev, status_code=404,
                               error="해제할 기록이 없습니다.")
            # 해제도 조치다 — 무엇을 되돌렸는지 이력에 남긴다.
            events.add_action(db, ev, events.ACT_MEMO, user=user,
                              memo=f"SOP 이행 해제 (단계 #{step_id})")
            msg = "이행 표시를 해제했습니다."
        else:
            try:
                row = SOP.check(db, event_id, step_id, user=user,
                                skipped=(op == "skip"), note=note)
            except ValueError as e:
                return _detail(request, db, user, ev, error=str(e),
                               status_code=400)
            verb = "해당 없음" if row.skipped else "이행"
            events.add_action(db, ev, events.ACT_MEMO, user=user,
                              memo=f"SOP {verb} — {row.step_title}"
                                   + (f" ({note})" if note else ""))
            msg = f"「{row.step_title}」을 {verb} 처리했습니다."

        db.commit()
        return _detail(request, db, user, ev, notice=msg)
