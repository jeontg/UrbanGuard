"""S-80 ~ S-84 설정 화면군.

지자체가 지점을 하나 늘릴 때마다 우리가 출동해야 하는 구조를 없애는 것이
이 화면들의 목적이다(설계서 5절).

⚠️ 이번 단계에서 편집 가능한 것은 **YAML 임계값·모델 설정**이다.
감시지점(S-80)과 ROI(S-81)는 조회만 제공하며, 이유는 각 화면에 명시했다.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..common.config import PROJECT_ROOT
from ..core import audit, config_edit
from ..core import roles as R
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

log = logging.getLogger("urbanguard.config")

CONFIG_DIR = PROJECT_ROOT / "configs"

# 화면 정의 — (키, 화면ID, 제목, 파일, 설명, 필요 권한, 도메인)
#
# ⚠️ "threshold"·"alert" 는 SETTINGS_OPS(도메인 종속)를 쓰면서도 그동안
#   ``domain=`` 을 넘기지 않아, 실제로는 **어느 MGR 이든** 이 화면을
#   드나들 수 있었다(2026-08-22 전수점검). 두 파일 다 내용을 보면 순수
#   침수(물 세그멘테이션) 전용이다 — risk_config.yaml 은 RiskEngine
#   가중치, alert_config.yaml 은 "침수 5단계 판정" 이라고 파일 자체에
#   명시돼 있다. 그래서 ``domain=Domain.FLOOD`` 를 고정으로 넘긴다.
SCREENS = {
    "threshold": {
        "sid": "S-82", "title": "위험도 임계값", "file": "risk_config.yaml",
        "desc": "위험등급 산정에 쓰는 가중치와 구간입니다. 바꾸면 전 지점의 등급이 함께 달라집니다.",
        "resource": R.SETTINGS_OPS, "domain": R.Domain.FLOOD,
    },
    "alert": {
        "sid": "S-83", "title": "알림 규칙", "file": "alert_config.yaml",
        "desc": "물 면적 비율·확산 속도 등 알림 단계 판정 기준입니다. 카메라별로 조정이 필요합니다.",
        "resource": R.SETTINGS_OPS, "domain": R.Domain.FLOOD,
    },
    "model": {
        "sid": "S-84", "title": "AI 모델 (물 세그멘테이션)", "file": "water_config.yaml",
        "desc": "물 탐지 모델 경로와 추론 설정입니다. 잘못 바꾸면 탐지가 멈춥니다.",
        "resource": R.SETTINGS_SYS,
    },
    "model-flood": {
        "sid": "S-84", "title": "AI 모델 (침수 파이프라인)",
        "file": "flood_model_config.yaml",
        "desc": "침수 파이프라인이 쓰는 모델·추적 설정입니다.",
        "resource": R.SETTINGS_SYS,
    },
    # 2026-08-24 신설 — 교통위험 판정 임계값(위 threshold/alert 와 같은
    # 이유로 SETTINGS_OPS + domain 고정). 화면번호는 아직 없다 — "교통위험"
    # 도메인 자체가 UI 설계서에 S-2x 를 아직 못 받은 것과 같은 사정
    # (docs/pending_tasks.md 에 확인 필요 항목으로 남김).
    "traffic": {
        "sid": "", "title": "교통위험 판정 임계값",
        "file": "traffic_risk_config.yaml",
        "desc": "강수·속도저하·정지차량 기준으로 교통위험 등급을 가르는 "
                "임계값입니다. 침수의 위험도 임계값(S-82)과 같은 방식입니다.",
        "resource": R.SETTINGS_OPS, "domain": R.Domain.TRAFFIC,
    },
}


def register(app, templates, base_ctx) -> None:

    # ---------- 공통 YAML 편집 화면 ----------
    def _yaml_page(request: Request, user: User, key: str, *, notice: str = "",
                   error: str = "", status_code: int = 200):
        spec = SCREENS[key]
        path = CONFIG_DIR / spec["file"]
        return templates.TemplateResponse(request, "settings_yaml.html", {
            **base_ctx(request, user), "active": f"set-{key}",
            "key": key, "spec": spec, "path_name": spec["file"],
            "exists": path.exists(),
            "fields": config_edit.scalar_fields(path) if path.exists() else [],
            "readonly": config_edit.readonly_fields(path) if path.exists() else [],
            "backups": config_edit.list_backups(path),
            "can_edit": R.can(user.role, spec["resource"], R.Action.EDIT),
            "notice": notice, "error": error,
        }, status_code=status_code)

    def _make_yaml_routes(key: str) -> None:
        spec = SCREENS[key]

        @app.get(f"/settings/{key}", response_class=HTMLResponse,
                 name=f"settings_{key}")
        def _get(request: Request,
                 user: User = Depends(require_page(spec["resource"],
                                                   R.Action.VIEW,
                                                   domain=spec.get("domain")))):
            return _yaml_page(request, user, key)

        @app.post(f"/settings/{key}", response_class=HTMLResponse,
                  name=f"settings_{key}_save")
        async def _post(request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_page(spec["resource"],
                                                          R.Action.EDIT,
                                                          domain=spec.get("domain")))):
            path = CONFIG_DIR / spec["file"]
            if not path.exists():
                return _yaml_page(request, user, key, status_code=404,
                                  error=f"설정 파일이 없습니다: {spec['file']}")
            form = await request.form()
            updates = {k[2:]: v for k, v in form.items() if k.startswith("f_")}
            try:
                res = config_edit.apply_updates(path, updates)
            except Exception as e:  # noqa: BLE001
                log.exception("설정 저장 실패 %s", path)
                return _yaml_page(request, user, key, status_code=500,
                                  error=f"저장에 실패했습니다: {str(e)[:120]}")

            if not res["changed"]:
                msg = "변경된 값이 없습니다."
                if res["skipped"]:
                    msg += f" (형식이 맞지 않아 건너뜀: {', '.join(res['skipped'])})"
                return _yaml_page(request, user, key, notice=msg)

            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target=f"{spec['title']} ({spec['file']})",
                         before={k: str(v[0]) for k, v in res["changed"].items()},
                         after={k: str(v[1]) for k, v in res["changed"].items()})
            db.commit()
            msg = f"{len(res['changed'])}개 값을 저장했습니다."
            if res["backup"]:
                msg += f" 이전 파일은 {res['backup'].name} 으로 백업했습니다."
            if res["skipped"]:
                msg += f" 형식이 맞지 않아 건너뛴 항목: {', '.join(res['skipped'])}"
            msg += " ⚠ 반영하려면 서비스를 재시작해야 합니다."
            return _yaml_page(request, user, key, notice=msg)

    for k in SCREENS:
        _make_yaml_routes(k)
    # S-80 감시지점·S-81 ROI 는 routes_blocks 모듈이 담당한다.
