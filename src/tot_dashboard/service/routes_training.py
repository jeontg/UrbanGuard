"""AI 모델 학습 — 관리자가 4개 탐지 도메인(침수·교통위험·인파관리·도로 노면)
모델을 화면에서 재학습할 수 있게 한다 (신규, 2026-08-25).

**배경** — ``docs/pending_tasks.md`` A-7(★★ 「재학습·MLOps — 성능 기록 표가
아예 없음」)과 「4대 탐지 기능 기술 정리」가 확인한 격차(교통위험·인파관리는
자체 학습 스크립트조차 없었음)를 함께 메운다. 실제 학습·데이터 판정 로직은
:mod:`..core.training_jobs`에 있다 — 이 파일은 화면·권한만 다룬다.

**왜 시스템관리자 전용인가** — 학습은 CPU를 몇 시간 점유해 관제 성능에
영향을 준다. 「AI 모델 운영(S-61)」·「AI 모델 설정(S-84)」과 같은 권한
(``SETTINGS_SYS``)으로 통일했다.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from sqlalchemy.orm import Session

from ..core import audit
from ..core import roles as R
from ..core import training_jobs as TJ
from ..core.auth import client_ip, get_db, require, require_page
from ..core.models import TrainingRun, User

log = logging.getLogger("urbanguard.training")


def _run_to_dict(run: TrainingRun) -> dict:
    return {
        "id": run.id, "domain": run.domain, "status": run.status,
        "params": run.params, "metrics": run.metrics, "error": run.error,
        "started_by_name": run.started_by_name,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def register(app, templates, base_ctx) -> None:

    def _page(request: Request, db: Session, user: User, *, domain: str = "flood",
              notice: str = "", error: str = "", status_code: int = 200):
        if domain not in TJ.DOMAINS:
            domain = "flood"
        busy = TJ.active_run(db)
        recent = {k: TJ.list_runs(db, k, limit=8) for k in TJ.DOMAINS}
        return templates.TemplateResponse(request, "settings_training.html", {
            # 도메인마다 화면 내용(탭)이 다르므로 강조 키도 도메인별로
            # 다르다 — 공유하면 침수 탭을 보면서 교통위험 메뉴까지 강조되는
            # 오작동이 생긴다(2026-08-25 실사용 중 발견).
            **base_ctx(request, user), "active": f"set-training-{domain}",
            "domains": TJ.DOMAINS, "cur_domain": domain,
            "dataset_status": {k: TJ.dataset_status(k) for k in TJ.DOMAINS},
            "busy": busy,
            # 다른 도메인이 실행 중일 때는 전체 로그 대신 진행률 한 줄만
            # 보여준다(2026-08-25, 사용자 요청 — "인파관리 탭인데 도로 노면
            # 로그가 그대로 보인다"는 지적). 내 도메인이 실행 중이면 어차피
            # 전체 로그를 보여주므로 진행률은 안 써도 된다.
            "busy_progress": TJ.progress_percent(busy) if busy else None,
            "recent_runs": recent,
            "auto_apply_note": TJ.AUTO_APPLY_NOTE,
            "notice": notice, "error": error,
        }, status_code=status_code)

    @app.get("/settings/training", response_class=HTMLResponse)
    def training_page(request: Request, domain: str = "flood",
                      db: Session = Depends(get_db),
                      user: User = Depends(require_page(R.SETTINGS_SYS,
                                                        R.Action.VIEW))):
        return _page(request, db, user, domain=domain)

    @app.post("/settings/training/start", response_class=HTMLResponse)
    async def training_start(request: Request,
                             db: Session = Depends(get_db),
                             user: User = Depends(require(R.SETTINGS_SYS,
                                                          R.Action.EXECUTE))):
        # 도메인별 파라미터 필드는 화면이 동적으로 만들므로, 등록된 필드
        # 이름만 폼에서 뽑아 params 딕셔너리로 모은다(request.form()은
        # 코루틴이라 async 핸들러여야 쓸 수 있다).
        form = await request.form()
        domain = (form.get("domain") or "").strip()
        if domain not in TJ.DOMAINS:
            return _page(request, db, user, domain="flood",
                        error="알 수 없는 도메인입니다.", status_code=400)
        spec = TJ.DOMAINS[domain]
        params = {}
        for f in spec.fields:
            raw = form.get(f.key)
            if raw is None:
                continue
            if f.type == "number":
                try:
                    params[f.key] = float(raw) if "." in str(raw) else int(raw)
                except ValueError:
                    params[f.key] = f.default
            else:
                params[f.key] = raw

        run, err = TJ.start(db, domain, params, user)
        if run is None:
            return _page(request, db, user, domain=domain, error=err,
                        status_code=409)
        audit.record_and_commit(
            db, action=audit.MODEL_TRAIN_START, user=user,
            target=f"{domain}#{run.id}", after=params, ip=client_ip(request),
            login_id=user.login_id, dept=getattr(user, "dept", ""))
        return _page(request, db, user, domain=domain,
                    notice=f"학습을 시작했습니다(실행 #{run.id}). "
                           "CPU 학습이라 수 시간이 걸릴 수 있습니다 — "
                           "이 화면을 닫아도 학습은 계속됩니다.")

    @app.post("/settings/training/stop", response_class=HTMLResponse)
    async def training_stop(request: Request,
                            db: Session = Depends(get_db),
                            user: User = Depends(require(R.SETTINGS_SYS,
                                                         R.Action.EXECUTE))):
        form = await request.form()
        try:
            run_id = int(form.get("run_id", "0"))
        except ValueError:
            run_id = 0
        run = db.get(TrainingRun, run_id)
        domain = run.domain if run else "flood"
        ok, err = TJ.stop(db, run_id)
        if not ok:
            return _page(request, db, user, domain=domain, error=err,
                        status_code=409)
        audit.record_and_commit(
            db, action=audit.MODEL_TRAIN_STOP, user=user,
            target=f"{domain}#{run_id}", ip=client_ip(request),
            login_id=user.login_id, dept=getattr(user, "dept", ""))
        return _page(request, db, user, domain=domain,
                    notice=f"실행 #{run_id}을 중지했습니다.")

    @app.get("/settings/training/status")
    def training_status(domain: str = "flood", db: Session = Depends(get_db),
                        user: User = Depends(require(R.SETTINGS_SYS,
                                                     R.Action.VIEW))) -> dict:
        """화면이 몇 초마다 불러 진행 상황을 갱신하는 폴링 API."""
        busy = TJ.active_run(db)
        if busy is not None:
            busy = TJ.refresh(db, busy)
        recent = TJ.list_runs(db, domain, limit=8)
        return {
            "busy": _run_to_dict(busy) if busy else None,
            "recent": [_run_to_dict(r) for r in recent],
            "log_tail": TJ.tail_log(busy, 120) if busy else "",
            # 다른 도메인 탭의 "진행률만" 표시용(전체 로그는 안 보낸다).
            "busy_progress": TJ.progress_percent(busy) if busy else None,
        }
