"""증거 영상 관리 화면 (S-88).

한 화면에서 세 가지를 한다 — **어느 등급을 남길지 정하고**, **등급별로
확인하고**, **지운다.**

⚠️ **여기서 다루는 파일은 개인정보다.** 그래서

* 열람도 권한을 요구한다(``EVIDENCE``/VIEW, ``core/guard.py`` 규칙표)
* 내려받기와 삭제는 **감사 로그에 남는다** — 누가 언제 무엇을 가져갔는지
* 삭제는 되돌릴 수 없어 부서담당자 이상만 할 수 있다
* 등급 설정은 **저장 정책**이라 시스템관리자(``SETTINGS_SYS``)가 맡는다
"""
from __future__ import annotations

import logging

from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core import events as E
from ..core import evidence as EV
from ..core import roles as R
from ..core import settings as ug_settings
from ..core.auth import client_ip, get_db, require_page
from ..core.models import Camera, Event, EventEvidence, User

log = logging.getLogger("urbanguard.evidence")

EVIDENCE_LEVELS_SET = "evidence.levels"
EVIDENCE_RETENTION_SET = "evidence.retention"
EVIDENCE_DOWNLOAD = "evidence.download"
# ★ **보기와 내려받기를 나눈다.**
#
#   화면에서 확인하는 것까지 「반출」로 적으면 대장이 노이즈로 가득 차
#   **정작 밖으로 나간 건을 못 찾는다.** 그렇다고 안 남기면 「누가 봤는가」가
#   사라진다 — 영상은 개인정보다. 그래서 **다른 코드로 둘 다 남긴다.**
EVIDENCE_VIEW = "evidence.view"
EVIDENCE_DELETE = "evidence.delete"

# 증거 자료가 어느 도메인 것인지 화면에 쓸 이름.
#
# ★ 표에 **도메인 칸이 없었다.** `EventEvidence.domain` 은 수집 때부터
#   채워지고 있었는데 화면이 안 보여 줘서, 관리자가 「이게 침수인지 인파인지」
#   를 알려면 이벤트 상세로 들어가야 했다.
# 짧은 이름은 roles 가 단일 출처다 — 여기에 따로 적어 두면 도메인이 늘 때
# 빠뜨린다(2026-08-21 flood/traffic 분리 때 실제로 그럴 뻔했다).
DOMAIN_LABELS = dict(R.DOMAIN_SHORT)

DEFAULT_LIMIT = 200


def register(app, templates, base_ctx) -> None:

    def _summary(db: Session) -> dict:
        """등급별 건수와 용량. **용량을 보여 주지 않으면 언제 지워야 할지
        판단할 수 없다.**"""
        rows = db.execute(
            select(EventEvidence.level, func.count(EventEvidence.id),
                   func.coalesce(func.sum(EventEvidence.bytes), 0))
            .group_by(EventEvidence.level)).all()
        per = {lv: {"count": 0, "bytes": 0} for lv in E.LEVELS}
        total = {"count": 0, "bytes": 0}
        for lv, n, b in rows:
            per.setdefault(lv or "미상", {"count": 0, "bytes": 0})
            per[lv or "미상"]["count"] = n
            per[lv or "미상"]["bytes"] = int(b or 0)
            total["count"] += n
            total["bytes"] += int(b or 0)
        return {"per": per, "total": total}

    def _page(request: Request, db: Session, user: User, *,
              notice: str = "", error: str = "", status_code: int = 200):
        p = request.query_params
        filt = {"level": p.get("level", ""), "kind": p.get("kind", ""),
                # 도메인 필터. 「침수 증거만 모아 보기」가 안 되던 것을 연다.
                "domain": p.get("domain", ""), "q": p.get("q", "")}

        stmt = (select(EventEvidence, Event)
                .join(Event, Event.id == EventEvidence.event_id, isouter=True))
        if filt["level"] in E.LEVELS:
            stmt = stmt.where(EventEvidence.level == filt["level"])
        if filt["kind"] in EV.KIND_LABELS:
            stmt = stmt.where(EventEvidence.kind == filt["kind"])
        if filt["domain"] in DOMAIN_LABELS:
            stmt = stmt.where(EventEvidence.domain == filt["domain"])
        q = filt["q"].strip()
        if q:
            like = f"%{q}%"
            stmt = stmt.where(EventEvidence.camera_id.ilike(like))
        stmt = stmt.order_by(EventEvidence.captured_at.desc(),
                             EventEvidence.id.desc()).limit(DEFAULT_LIMIT)
        rows = [{"ev": e, "event": evt} for e, evt in db.execute(stmt).all()]

        return templates.TemplateResponse(request, "admin_evidence.html", {
            **base_ctx(request, user), "active": "evidence",
            "rows": rows, "summary": _summary(db),
            "levels": E.LEVELS,
            "domain_labels": DOMAIN_LABELS,
            "event_threshold": E.EVENT_THRESHOLD,
            # 임계값보다 낮아 **이벤트로 올라오지 않는** 등급. 고를 수는
            # 있게 두되, 골라도 안 남는다는 사실을 화면이 말해야 한다.
            "dead_levels": [lv for lv in E.LEVELS if not E.is_reportable(lv)],
            "selected": ug_settings.evidence_levels(db),
            # 보존기간(개월). 0이면 자동 파기하지 않는다.
            "retention_months": ug_settings.evidence_retention_months(db),
            "retention_choices": ug_settings.RETENTION_CHOICES,
            "expiring_at": EV.months_ago(
                ug_settings.evidence_retention_months(db)),
            "kind_labels": EV.KIND_LABELS,
            "human_size": EV.human_size,
            "filt": filt,
            "pre_sec": EV.PRE_SEC, "post_sec": EV.POST_SEC, "fps": EV.FPS,
            "active_jobs": EV.active_count(),
            "can_delete": R.can(user.role, R.EVIDENCE, R.Action.EDIT),
            "can_configure": R.can(user.role, R.SETTINGS_SYS, R.Action.EDIT),
            "notice": notice or p.get("notice", ""),
            "error": error or p.get("error", ""),
        }, status_code=status_code)

    @app.get("/admin/evidence", response_class=HTMLResponse)
    def evidence_page(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.EVIDENCE,
                                                        R.Action.VIEW))):
        return _page(request, db, user)

    def _back(request: Request, notice: str = "", error: str = ""):
        params = [(k, v) for k, v in request.query_params.multi_items()
                  if k not in ("notice", "error")]
        if notice:
            params.append(("notice", notice))
        elif error:
            params.append(("error", error))
        qs = urlencode(params)
        return RedirectResponse(url=f"/admin/evidence{'?' + qs if qs else ''}",
                                status_code=303)

    # ---------- 등급 설정 (시스템관리자) ----------
    @app.post("/admin/evidence/levels", response_class=HTMLResponse)
    def evidence_levels(request: Request, levels: list[str] = Form(default=[]),
                        db: Session = Depends(get_db),
                        user: User = Depends(require_page(R.SETTINGS_SYS,
                                                          R.Action.EDIT))):
        before = ",".join(sorted(ug_settings.evidence_levels(db)))
        value = ug_settings.set_evidence_levels(db, levels)
        db.commit()
        ug_settings.load_all(db)
        audit.record_and_commit(
            db, action=EVIDENCE_LEVELS_SET, user=user, ip=client_ip(request),
            target="증거 영상 저장 등급", before={"levels": before},
            after={"levels": value})
        if not value:
            return _back(request,
                         notice="증거 영상을 남기지 않도록 설정했습니다. "
                                "앞으로 발생하는 이벤트에는 근거 영상이 없습니다.")
        msg = f"「{value.replace(',', ' · ')}」 등급에서 증거 영상을 남깁니다."
        # 「관심」은 이벤트로 올라오지 않으므로 골라도 아무것도 안 남는다.
        # 화면이 이 사실을 말하지 않으면 「설정했는데 왜 없지」가 된다.
        picked = {p.strip() for p in value.split(",")}
        dead = [lv for lv in picked if not E.is_reportable(lv)]
        if dead:
            msg += (f" ⚠ 다만 「{' · '.join(dead)}」 은(는) 이벤트로 올리지 않는 "
                    f"등급이라(기준: {E.EVENT_THRESHOLD}) 실제로는 남지 않습니다.")
        return _back(request, notice=msg)

    @app.post("/admin/evidence/retention", response_class=HTMLResponse)
    def evidence_retention(request: Request, months: str = Form("0"),
                           db: Session = Depends(get_db),
                           user: User = Depends(require_page(R.SETTINGS_SYS,
                                                             R.Action.EDIT))):
        before = ug_settings.evidence_retention_months(db)
        n = ug_settings.set_evidence_retention_months(db, months)
        db.commit()
        ug_settings.load_all(db)
        audit.record_and_commit(
            db, action=EVIDENCE_RETENTION_SET, user=user,
            ip=client_ip(request), target="증거 영상 보존기간",
            before={"months": before}, after={"months": n})
        if n <= 0:
            return _back(request,
                         notice="자동 파기를 끕니다. 보존기간이 지나도 자료가 "
                                "지워지지 않으므로 직접 정리하셔야 합니다.")
        # **얼마나 지워질지 미리 알려 준다.** 「3개월」을 골랐는데 그 자리에서
        # 수백 건이 사라지면, 몰랐다는 말이 나온다.
        cutoff = EV.months_ago(n)
        doomed = db.execute(
            select(func.count(EventEvidence.id))
            .where(EventEvidence.captured_at < cutoff)).scalar() or 0
        msg = (f"보존기간을 {n}개월로 설정했습니다. "
               f"{cutoff.strftime('%Y-%m-%d')} 이전 자료가 파기 대상입니다.")
        if doomed:
            msg += f" ⚠ 지금 기준으로 {doomed}건이 곧 자동 파기됩니다."
        else:
            msg += " 현재 파기 대상은 없습니다."
        return _back(request, notice=msg)

    # ---------- 내려받기 ----------
    @app.get("/admin/evidence/{ev_id}/file")
    def evidence_file(ev_id: int, request: Request, inline: int = 0,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.EVIDENCE,
                                                        R.Action.VIEW))):
        row = db.get(EventEvidence, ev_id)
        if row is None:
            return _back(request, error="대상 자료를 찾을 수 없습니다.")
        try:
            path = EV.abs_path(row.path)
        except ValueError:
            return _back(request, error="저장 경로가 올바르지 않습니다.")
        if not path.exists():
            return _back(request, error="파일이 없습니다. 이미 삭제된 자료입니다.")
        # **가져간 사실을 남긴다.** 영상은 개인정보이고, 밖으로 나가면
        # 반출 관리대장(S-94) 대상이 된다.
        audit.record_and_commit(
            db, action=(EVIDENCE_VIEW if inline else EVIDENCE_DOWNLOAD),
            user=user, ip=client_ip(request),
            target=f"evidence:{ev_id} 이벤트 #{row.event_id}",
            after={"kind": row.kind, "level": row.level,
                   "camera": row.camera_id, "sha256": row.sha256[:16]})
        # ⚠️ 확장자로 정한다 — **예전 자료는 .mp4(mp4v), 지금은 .webm(vp09)**
        #    이다(core/evidence.py 2026-08-20). 종류만 보고 media_type 을
        #    고정하면 예전 mp4v 파일도 `video/webm` 으로 내보내 브라우저가
        #    거부하거나, 반대로 새 webm 을 `video/mp4` 로 내보내 재생이
        #    안 될 수 있다.
        media = ("image/jpeg" if row.kind == EV.KIND_IMAGE
                 else "video/webm" if path.suffix == ".webm" else "video/mp4")
        if inline:
            # ⚠️ `filename=` 을 주면 FileResponse 가
            #    `Content-Disposition: attachment` 를 붙여 **브라우저가 내려받는다.**
            #    팝업에서 바로 보려면 inline 이어야 한다.
            return FileResponse(path, media_type=media)
        return FileResponse(path, media_type=media, filename=path.name)


    @app.get("/api/evidence/{ev_id}/meta")
    def evidence_meta(ev_id: int, db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.EVIDENCE,
                                                        R.Action.VIEW))):
        """팝업이 「이벤트가 어디서·언제 났는지」를 그리는 데 필요한 값.

        ★ **세 가지를 준다.**

        1. ``roi`` — 그 지점·도메인의 감시 구역. 화면 위에 겹쳐 그린다
        2. ``event_at_sec`` — 클립 안에서 **이벤트 순간이 몇 초 지점인지**
        3. ``boxes`` — **실제로 관측된** 탐지 상자(2026-08-20 부터 저장).
           도메인마다 뜻이 다르다 — 침수는 세그멘테이션이 판정한 물 영역의
           경계, 인파는 그 이벤트를 일으킨 트랙의 추적 상자, 노면은 YOLO 가
           찾은 손상 상자다. **지어낸 값이 아니다.**

        ⚠️ **`has_box` 를 정직하게 답한다.** 저장 이전에 수집된 자료나, 그
        틱에 아무것도 못 찾은 경우는 ``boxes`` 가 비어 있다 — 그때는 없다고
        말해야지 지어내면 안 된다. ROI 는 「이 구역을 보고 있었다」는
        뜻이지 「여기서 났다」가 아니다 — 화면이 그 차이도 말해야 한다.
        """
        row = db.get(EventEvidence, ev_id)
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)

        # 클립은 이벤트 시각 앞뒤로 자른다. 그래서 **앞 구간 길이가 곧
        # 이벤트 순간**이다(core/evidence.py 의 PRE_SEC/POST_SEC).
        event_at = EV.PRE_SEC if row.kind == EV.KIND_CLIP else 0

        roi = None
        try:
            cam = db.get(Camera, row.camera_id) if row.camera_id else None
            rrow = cam.roi_row(row.domain) if cam is not None else None
            if rrow is not None and rrow.frame_width and rrow.frame_height:
                roi = {"frame_width": rrow.frame_width,
                       "frame_height": rrow.frame_height,
                       "shapes": rrow.shapes or {}}
        except Exception:  # noqa: BLE001
            # ⚠️ ROI 를 못 읽었다고 팝업이 안 열리면 안 된다. 없으면 없다고
            #    말하고 영상은 그대로 보여 준다.
            log.debug("증거 ROI 조회 실패 ev=%s", ev_id, exc_info=True)
            roi = None

        ev = db.get(Event, row.event_id) if row.event_id else None
        return JSONResponse({
            "id": row.id, "kind": row.kind, "domain": row.domain,
            "domain_label": DOMAIN_LABELS.get(row.domain, row.domain or "—"),
            "level": row.level, "camera_id": row.camera_id,
            "duration_sec": row.duration_sec,
            "event_at_sec": event_at,
            "captured_at": row.captured_at.isoformat() if row.captured_at else "",
            "event_id": row.event_id,
            "place_name": getattr(ev, "place_name", "") or "",
            "event_type": getattr(ev, "event_type", "") or "",
            "detail": getattr(ev, "detail", None) or {},
            "roi": roi,
            # ★ 실제로 저장된 값만 준다 — 없으면 빈 배열이고 `has_box` 는
            #   거짓이다. 화면이 이 값으로 안내 문구를 고른다.
            "boxes": row.boxes or [],
            "frame_w": row.frame_w or 0,
            "frame_h": row.frame_h or 0,
            "has_box": bool(row.boxes),
        })

    # ---------- 삭제 ----------
    def _delete_rows(db: Session, rows: list[EventEvidence]) -> tuple[int, int]:
        """행과 파일을 함께 지운다. (건수, 바이트)."""
        n = size = 0
        for r in rows:
            EV.remove_file(r.path)
            size += r.bytes or 0
            n += 1
        ids = [r.id for r in rows]
        if ids:
            db.execute(sa_delete(EventEvidence)
                       .where(EventEvidence.id.in_(ids)))
        return n, size

    @app.post("/admin/evidence/delete", response_class=HTMLResponse)
    def evidence_delete(request: Request, ids: list[int] = Form(default=[]),
                        db: Session = Depends(get_db),
                        user: User = Depends(require_page(R.EVIDENCE,
                                                          R.Action.EDIT))):
        if not ids:
            return _back(request, error="선택한 자료가 없습니다.")
        rows = list(db.scalars(select(EventEvidence)
                               .where(EventEvidence.id.in_(ids))).all())
        # 지우기 전에 무엇을 지우는지 읽어 둔다 — 지운 뒤에는 알 수 없다.
        detail = [{"id": r.id, "event": r.event_id, "kind": r.kind,
                   "level": r.level, "camera": r.camera_id} for r in rows]
        n, size = _delete_rows(db, rows)
        db.commit()
        audit.record_and_commit(
            db, action=EVIDENCE_DELETE, user=user, ip=client_ip(request),
            target=f"증거 자료 {n}건", before={"deleted": detail})
        return _back(request,
                     notice=f"{n}건({EV.human_size(size)})을 삭제했습니다. "
                            "되돌릴 수 없습니다.")

    @app.post("/admin/evidence/delete-level", response_class=HTMLResponse)
    def evidence_delete_level(request: Request, level: str = Form(""),
                              db: Session = Depends(get_db),
                              user: User = Depends(require_page(
                                  R.EVIDENCE, R.Action.EDIT))):
        """등급 단위 일괄 삭제. 「주의만 정리」 같은 실무 요구가 있다."""
        if level not in E.LEVELS:
            return _back(request, error="등급을 고르세요.")
        rows = list(db.scalars(select(EventEvidence)
                               .where(EventEvidence.level == level)).all())
        if not rows:
            return _back(request, error=f"「{level}」 등급 자료가 없습니다.")
        n, size = _delete_rows(db, rows)
        db.commit()
        audit.record_and_commit(
            db, action=EVIDENCE_DELETE, user=user, ip=client_ip(request),
            target=f"증거 자료 등급 일괄 「{level}」 {n}건",
            before={"level": level, "count": n, "bytes": size})
        return _back(request,
                     notice=f"「{level}」 등급 {n}건({EV.human_size(size)})을 "
                            "삭제했습니다. 되돌릴 수 없습니다.")
