"""S-60 통계·성과 리포트 · S-61 AI 모델 운영."""
from __future__ import annotations

import csv
import io
import logging
import re

from fastapi import Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..core import analytics as A
from ..core import audit
from ..core import model_ops
from ..core import model_probe as probe_mod
from ..core import model_registry as registry
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

log = logging.getLogger("urbanguard.analytics")

# 미리보기 파일명은 우리가 만든 16진수 uuid 뿐이다. 그 밖의 것은 받지 않는다.
_PREVIEW_NAME = re.compile(r"^[0-9a-f]{32}\.jpg$")


def _scope(user: User) -> set[str] | None:
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def register(app, templates, base_ctx) -> None:

    # ---------- S-60 통계 ----------
    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request, period: str = A.DEFAULT_PERIOD,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.AUDIT, R.Action.VIEW))):
        scope = _scope(user)
        daily = A.daily_counts(db, period, scope)
        peak = max((d["count"] for d in daily), default=0)
        return templates.TemplateResponse(request, "analytics.html", {
            **base_ctx(request, user), "active": "analytics",
            "s": A.summary(db, period, scope), "daily": daily, "peak": peak or 1,
            "periods": [(k, v[0]) for k, v in A.PERIODS.items()],
            "sel_period": period,
            # Phase 3(2026-08-26) — 유형별 오탐률. 백엔드는 새로 만들지 않고
            # 이미 쌓이던 판정(S-07)을 유형 단위로 묶어 노출만 한다.
            "quality": A.detection_quality(db, period, scope),
            "quality_min_sample": A.MIN_SAMPLE,
        })

    @app.get("/analytics/export")
    def analytics_export(request: Request, period: str = A.DEFAULT_PERIOD,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_page(R.AUDIT, R.Action.VIEW))):
        """CSV 내보내기 — 성과 보고서에 붙일 수 있어야 실무에 쓰인다."""
        s = A.summary(db, period, _scope(user))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["항목", "값"])
        w.writerow(["기간", s["period_label"]])
        w.writerow(["집계 시작", s["since"].strftime("%Y-%m-%d")])
        w.writerow(["총 탐지 건수", s["total"]])
        w.writerow(["종결", s["closed"]])
        w.writerow(["진행 중", s["open"]])
        w.writerow(["조치 완료율(%)", s["close_rate"]])
        w.writerow(["오탐 신고", s["false_positive"]])
        w.writerow(["오탐률(%)", s["false_positive_rate"]])
        w.writerow(["평균 확인 소요(분)", s["ack_avg_min"] if s["ack_avg_min"] is not None else "—"])
        w.writerow(["최대 확인 소요(분)", s["ack_max_min"] if s["ack_max_min"] is not None else "—"])
        w.writerow(["통보 요청", s["notify_requested"]])
        w.writerow(["통보 발송", s["notify_sent"]])
        w.writerow([])
        w.writerow(["도메인", "건수"])
        for d in s["by_domain"]:
            w.writerow([d["label"], d["count"]])
        w.writerow([])
        w.writerow(["최고 등급", "건수"])
        for lv in s["by_level"]:
            w.writerow([lv["level"], lv["count"]])
        w.writerow([])
        # Phase 3(2026-08-26) — 유형별 오탐률. 관제요원이 판정한 건만 센다.
        w.writerow(["유형별 오탐률(관제요원 판정 기준, 재현율 아님)"])
        w.writerow(["도메인", "위험유형", "판정 건수", "오탐률(%)", "비고"])
        quality = A.detection_quality(db, period, _scope(user))
        for q in quality:
            w.writerow([
                q["domain_label"], q["label"], q["judged"],
                q["false_rate"] if q["false_rate"] is not None else "—",
                f"표본 {q['judged']}건 — {A.MIN_SAMPLE}건 미만은 참고용"
                if q["low_sample"] else ""])

        audit.record_and_commit(db, action=audit.REPORT_EXPORT, user=user,
                                ip=client_ip(request),
                                target=f"통계 CSV 내보내기 ({s['period_label']})")
        # 엑셀이 UTF-8 CSV 를 cp949 로 읽어 한글이 깨지므로 BOM 을 붙인다.
        data = "﻿" + buf.getvalue()
        return StreamingResponse(
            iter([data]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="urbanguard_stats_{period}.csv"'})

    # ---------- S-61 모델 운영 ----------
    def _road_gap(db: Session) -> dict:
        """노면 모델이 실무 기준과 어긋나는 지점을 **지금 상태에서 센다.**

        서울특별시 도로포장 유지관리 매뉴얼(2018)의 「서울형 Decision Tree」는
        **균열률 CR(면적 %)** 과 소성변형 RD(mm), 평탄성 IRI(m/km) 로 보수공법을
        정한다. 우리가 내는 값은 **개수(건/100m)** 라 그 표에 넣을 수 없다.

        이 사실을 화면에 적어 두는 이유 — 「AI 노면 탐지가 된다」와 「기관
        기준으로 판정할 수 있다」는 다른 말이다. 그 차이를 화면이 숨기면
        제안 단계에서 과대 약속이 된다.
        """
        try:
            from ..core import calibration as CAL
            from ..core import cameras as C
            cams = C.for_domain(db, "road", continuous=None)
            calibrated = sum(1 for c in cams
                             if CAL.of(c, "road").section is not None)
        except Exception as e:  # noqa: BLE001
            log.warning("노면 보정 현황 조회 실패: %s", str(e)[:120])
            cams, calibrated = [], 0
        return {
            "cameras": len(cams),
            "calibrated": calibrated,
            "uncalibrated": len(cams) - calibrated,
        }

    def _models_page(request: Request, db: Session, user: User, *,
                     probe: dict | None = None, notice: str = "",
                     error: str = "", status_code: int = 200):
        # 도메인별로 「고를 수 있는 모델」과 「대 볼 대상」을 함께 내린다.
        # 어느 하나가 실패해도 화면은 떠야 하므로 각각 감싼다.
        bench = []
        # 2026-08-21 flood/traffic 도메인 분리로 「교통」이 추가됐다.
        for dom, label in (("flood", "침수"), ("traffic", "교통"),
                           ("crowd", "인파"), ("road", "노면")):
            try:
                models = [m.to_dict() for m in registry.for_domain(dom)]
            except Exception as e:  # noqa: BLE001
                log.warning("모델 목록 조회 실패 %s: %s", dom, str(e)[:120])
                models = []
            try:
                tgts = probe_mod.targets(dom)
            except Exception as e:  # noqa: BLE001
                log.warning("시험 대상 조회 실패 %s: %s", dom, str(e)[:120])
                tgts = []
            bench.append({"domain": dom, "label": label, "models": models,
                          "targets": tgts,
                          # 기본 선택은 **지금 운영이 쓰는 모델**이다. 시험대에서
                          # 다른 것을 고르기 전까지는 운영과 같은 조건이어야
                          # 비교의 출발점이 된다.
                          "default_key": model_ops.selected_key(dom, db),
                          "live_apply": dom in model_ops.LIVE_APPLY,
                          "apply_note": model_ops.APPLY_NOTE.get(dom, ""),
                          "note": model_ops.note(dom, db)})
        return templates.TemplateResponse(request, "models.html", {
            **base_ctx(request, user), "active": "models",
            "rows": A.model_stats(db),
            "training": A.training_data_status(db),
            "fp_events": A.false_positive_events(db),
            "bench": bench, "probe": probe, "busy": probe_mod.busy(),
            # 노면 모델이 실무 기준과 어디서 어긋나는지. **숫자를 박아 두지
            # 않고 지금 상태에서 센다** — 보정을 끝내면 화면도 따라 바뀐다.
            "road_gap": _road_gap(db),
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_page(R.SETTINGS_SYS,
                                                      R.Action.VIEW))):
        return _models_page(request, db, user)

    @app.post("/models/probe", response_class=HTMLResponse)
    async def models_probe(request: Request, domain: str = Form(...),
                           model_key: str = Form(...), target: str = Form(...),
                           kind: str = Form("cctv"),
                           duration_sec: float = Form(
                               probe_mod.DEFAULT_DURATION_SEC),
                           conf: float = Form(0.25),
                           db: Session = Depends(get_db),
                           user: User = Depends(require_page(R.SETTINGS_SYS,
                                                             R.Action.EDIT))):
        """고른 모델로 한 지점을 관측한다 — **시험일 뿐 운영에 반영되지 않는다.**

        스트림을 몇 초 동안 붙들고 추론까지 하므로 응답이 그만큼 늦다.
        이벤트 루프를 막지 않도록 스레드풀로 넘긴다.
        """
        try:
            result = await run_in_threadpool(
                probe_mod.run, domain, model_key, target,
                kind=kind, duration_sec=duration_sec, conf=conf)
        except RuntimeError as e:          # 이미 다른 시험이 도는 중
            return _models_page(request, db, user, error=str(e), status_code=409)
        except (LookupError, FileNotFoundError, ValueError) as e:
            return _models_page(request, db, user, error=str(e), status_code=400)
        except Exception as e:  # noqa: BLE001
            log.exception("시험 탐지 실패 domain=%s model=%s", domain, model_key)
            return _models_page(request, db, user, status_code=500,
                                error=f"시험 탐지에 실패했습니다: {str(e)[:160]}")

        audit.record_and_commit(
            db, action=audit.DETECT_RUN, user=user, ip=client_ip(request),
            target=f"모델 시험 {domain}:{result.target_name}",
            after={"model": result.model_key, "frames": result.frames_analyzed,
                   "metrics": result.metrics})
        return _models_page(request, db, user, probe=result.to_dict())

    @app.post("/models/select", response_class=HTMLResponse)
    def models_select(request: Request, domain: str = Form(...),
                      model_key: str = Form(...),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.SETTINGS_SYS,
                                                        R.Action.EDIT))):
        """상시 탐지가 쓸 모델을 정한다.

        반영 시점이 도메인마다 다르다 — 그 차이를 감추지 않고 그대로 알린다.
        「저장했습니다」로 끝내면 바뀐 줄 알고 관제하게 된다.
        """
        try:
            res = model_ops.choose(db, domain, model_key)
        except (LookupError, FileNotFoundError, ValueError) as e:
            return _models_page(request, db, user, error=str(e), status_code=400)

        audit.record_and_commit(
            db, action=audit.SETTINGS_UPDATE, user=user, ip=client_ip(request),
            target=f"운영 모델 · {domain}",
            before={"model": res["before"]},
            after={"model": res["model"]["key"], "applied": res["applied"]})

        head = f"{domain} 운영 모델을 «{res['model']['label']}» 로 지정했습니다."
        tail = ("지금 바로 반영됐습니다 — 다음 순회부터 새 모델이 돕니다."
                if res["applied"] else res["note"])
        return _models_page(request, db, user, notice=f"{head} {tail}")

    @app.post("/models/note", response_class=HTMLResponse)
    def models_note(request: Request, domain: str = Form(...),
                    note: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(require_page(R.SETTINGS_SYS,
                                                      R.Action.EDIT))):
        """모델 비고 — 파일이나 통계에서 도출되지 않는 **사람의 판단**을 적는다."""
        try:
            before = model_ops.set_note(db, domain, note)
        except ValueError as e:
            return _models_page(request, db, user, error=str(e), status_code=400)
        audit.record_and_commit(
            db, action=audit.SETTINGS_UPDATE, user=user, ip=client_ip(request),
            target=f"모델 비고 · {domain}",
            before={"note": before[:200]}, after={"note": note[:200]})
        return _models_page(request, db, user, notice="비고를 저장했습니다.")

    @app.get("/models/preview/{name}")
    def models_preview(name: str,
                       user: User = Depends(require_page(R.SETTINGS_SYS,
                                                         R.Action.VIEW))):
        """시험 미리보기 이미지. 사람은 저장 전에 이미 가려져 있다."""
        # 경로 조작 방지 — 파일명만 받고 디렉터리 구분자는 허용하지 않는다.
        if not _PREVIEW_NAME.match(name):
            raise HTTPException(status_code=404, detail="없는 이미지입니다.")
        path = probe_mod.PREVIEW_DIR / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="없는 이미지입니다.")
        return FileResponse(path, media_type="image/jpeg")
