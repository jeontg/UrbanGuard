"""S-95 위험등급 관리 · S-96 지점 방향 관리 · S-97 상·하류 관리.

세 화면 모두 **관계 모델(Urban Ontology)을 관리자가 손으로 다루는 자리**다.
지금까지는 코드에 박혀 있거나(등급 이름·구간), 지점 하나씩 수정 폼을 열어야
했다(방향각). 39지점을 하나씩 여는 것은 현실적이지 않다.

⚠️ 침수 단계 어휘를 4등급으로 통일하면서 **구간 숫자를 표로 뺐다.** 근거는
화면에 함께 띄운다 — 기관이 바꾸더라도 원래 무엇을 근거로 정한 값이었는지는
남아야 한다.
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core import cameras as C
from ..core import relations as REL
from ..core import roles as R
from ..core import settings as ug_settings
from ..core import vocabulary as V
from ..core.auth import client_ip, get_db, require_page
from ..core.models import Camera, LevelThreshold, RiskLevel, User

log = logging.getLogger("urbanguard.levels")

# 구간을 관리하는 도메인.
#
# ★ 2026-08-19 오후 **노면을 넣었다.** 그전에는 노면 구간값(1·3·6 건/100m)이
#   calibration.py 에 상수로 박혀 있어 **기관이 화면에서 바꿀 수 없었다.**
#   침수·인파는 바꿀 수 있는데 노면만 못 바꾸는 것은 이유가 없다.
#
# ⚠️ 다만 **같은 표에 나란히 놓지 않는다.** 노면은 위험등급이 아니라
#   **정비 등급**이라(vocabulary.KIND_MAINTENANCE) 화면에서 묶음을 나누고
#   성격 설명을 함께 띄운다. 등급 이름을 통일하지 않기로 한 결정
#   (2026-08-19)은 그대로다 — **이름은 그대로 두고 숫자만 열었다.**
# ⚠️ **교통은 여기 없다.** 교통 판정은 강우량 × 속도저하 × 정지차량의
#   다변량 조합이라 `(도메인, 등급, 최소값, 단위)` 구조인 level_thresholds
#   에 담을 수 없다 — 억지로 넣으면 화면에 뜨는 「근거 값」이 실제 판정과
#   달라진다(2026-08-21 도메인 분리, docs/202608210801 5절).
THRESHOLD_DOMAINS = [
    ("flood", "침수", "침수심(cm)"),
    ("crowd", "인파관리", "밀도(명/㎡)"),
    ("road", "도로 노면 관리", "손상 밀도(건/100m)"),
]

# 위험등급 묶음과 정비 등급 묶음을 화면에서 가른다.
RISK_DOMAINS = [d for d in THRESHOLD_DOMAINS if d[0] != "road"]
MAINT_DOMAINS = [d for d in THRESHOLD_DOMAINS if d[0] == "road"]


def _scope(user: User) -> set[str] | None:
    """MGR 만 담당 도메인으로 제한된다. SYS 는 전 도메인(``None``).

    이 파일의 라우트 전부가 ``domain=`` 을 안 넘겨 실제로는 전혀 걸러지지
    않던 문제를 2026-08-22 전수점검에서 발견해 고친다
    (``routes_cameras.py``/``routes_events.py`` 와 같은 패턴).
    """
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def register(app, templates, base_ctx) -> None:

    # ---------- S-95 위험등급 관리 ----------
    def _levels_page(request: Request, db: Session, user: User, *,
                     notice: str = "", errors: list[str] | None = None,
                     status_code: int = 200):
        # ⚠️ MGR 은 담당 도메인의 구간·설정만 본다 — 못 고칠 것을 보여주고
        # 저장 때 403 을 내는 것보다, 애초에 안 보여주는 편이 낫다
        # (2026-08-22 전수점검). SYS 는 전체를 그대로 본다.
        scope = _scope(user)
        risk_domains = ([d for d in RISK_DOMAINS if d[0] in scope]
                        if scope is not None else RISK_DOMAINS)
        maint_domains = ([d for d in MAINT_DOMAINS if d[0] in scope]
                         if scope is not None else MAINT_DOMAINS)
        return templates.TemplateResponse(request, "settings_levels.html", {
            **base_ctx(request, user), "active": "set-levels",
            "levels": V.active_levels(db),
            # 위험등급 어휘와 정비 등급 어휘를 **따로** 넘긴다. 한 목록으로
            # 합치면 화면에서 「심각」과 「긴급」이 나란히 서서, 이름을
            # 통일하지 않기로 한 결정이 무의미해진다.
            "maint_levels": V.active_levels(db, V.KIND_MAINTENANCE),
            "domains": [(k, label, unit, V.threshold_rows(db, k))
                        for k, label, unit in risk_domains],
            "maint_domains": [(k, label, unit, V.threshold_rows(db, k))
                              for k, label, unit in maint_domains],
            "can_edit_levels": scope is None,  # RiskLevel 이름·색 = SYS 전용
            # 2026-08-21 flood/traffic 도메인 분리 — 침수 위험도 독립 알림의
            # 발동 등급. 구간표와 달리 도메인당 값 하나뿐이라 별도 폼으로 둔다.
            "flood_notify_min_grade": ug_settings.flood_notify_min_grade(db),
            # 2026-08-26 — 교통·인파·노면도 같은 방식으로 추가.
            "traffic_notify_min_severity": ug_settings.traffic_notify_min_severity(db),
            "crowd_density_min_severity": ug_settings.crowd_density_min_severity(db),
            "road_event_min_grade": ug_settings.road_event_min_grade(db),
            # 2026-08-27 — 강수(rainfall) 데이터 출처. 침수·교통위험 판정이
            # 함께 쓰는 전역값이라 도메인 하나로 스코프하지 않는다(아래 SYS
            # 전용 처리와 같은 이유, S-95 다른 전역 재계산과 동일 패턴).
            "rainfall_backend": ug_settings.rainfall_backend(db),
            "rainfall_backends": ug_settings.RAINFALL_BACKENDS,
            "kma_key_present": bool(os.environ.get("KMA_SERVICE_KEY")),
            # 2026-08-27 — 인파 상시 카메라별 모니터링의 전역 검출 소스.
            "crowd_continuous_source": ug_settings.crowd_continuous_source(db),
            "crowd_continuous_sources": ug_settings.CROWD_CONTINUOUS_SOURCES,
            # 2026-08-29 — 인파 검출 타일 격자 전역 기본값.
            "crowd_tile_grid": ug_settings.crowd_tile_grid(db),
            "crowd_tile_grid_options": ug_settings.CROWD_TILE_GRID_OPTIONS,
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/settings/levels", response_class=HTMLResponse)
    def levels_page(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_page(R.SETTINGS_OPS,
                                                      R.Action.VIEW))):
        return _levels_page(request, db, user)

    @app.post("/settings/levels/label", response_class=HTMLResponse)
    async def levels_label(request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_page(R.SETTINGS_OPS,
                                                             R.Action.EDIT))):
        """등급 이름과 색을 고친다.

        ⚠️ **code 와 seq 는 못 바꾼다.** code 는 다른 표가 참조하고, seq 는
        비교의 유일한 기준이다. 이름만 바꾸는 것이 이 화면의 목적이다 —
        기관이 「심각」을 「위험」이라 부르더라도 판정은 그대로 맞아야 한다.

        ⚠️ **시스템관리자 전용.** ``RiskLevel`` 은 도메인 구분이 없는
        전역 어휘(관심·주의·경계·심각 그 자체)라 특정 부서 소관으로 나눌
        수 없다(2026-08-22 전수점검 — 예전에는 MGR 도 걸러지지 않고 다
        고칠 수 있었다).
        """
        if user.role != R.Role.SYS.value:
            raise HTTPException(status_code=403,
                                detail="등급 이름·색은 시스템관리자만 고칠 수 있습니다.")
        form = await request.form()
        rows = db.scalars(select(RiskLevel)).all()
        errs: list[str] = []
        changed: list[str] = []
        for r in rows:
            # ⚠️ **폼에 없는 칸은 건드리지 않는다.** 없는 것과 비운 것은 다르다.
            #    노면 정비 등급이 표에 들어오면서(2026-08-19) 이 폼이 그리지
            #    않는 행이 생겼는데, 없는 것을 「비웠다」로 읽어 **이름 변경이
            #    항상 400 으로 막혔다.** 시험이 잡았다.
            if f"label_{r.code}" not in form:
                continue
            label = (form.get(f"label_{r.code}") or "").strip()
            color = (form.get(f"color_{r.code}") or "").strip()
            if not label:
                errs.append(f"「{r.label}」 등급의 이름을 비울 수 없습니다.")
                continue
            if len(label) > 32:
                errs.append(f"등급 이름이 너무 깁니다: {label[:20]}…")
                continue
            if label != r.label or color != r.color:
                changed.append(f"{r.label}→{label}")
                r.label, r.color = label, color
        if errs:
            db.rollback()
            return _levels_page(request, db, user, errors=errs, status_code=400)
        if changed:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="위험등급 이름 변경",
                         after={"changed": changed})
        db.commit()
        return _levels_page(request, db, user, notice=(
            f"등급 이름을 고쳤습니다({len(changed)}건). "
            "⚠ 지난 이벤트에 저장된 등급 문자열은 그대로 남습니다."
            if changed else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/threshold", response_class=HTMLResponse)
    async def levels_threshold(request: Request, db: Session = Depends(get_db),
                               user: User = Depends(require_page(
                                   R.SETTINGS_OPS, R.Action.EDIT))):
        """등급 구간(몇 부터 어느 등급인가)을 고친다.

        ⚠️ MGR 은 담당 도메인의 구간만 고칠 수 있다(2026-08-22 전수점검).
        """
        form = await request.form()
        rows = db.scalars(select(LevelThreshold)).all()
        scope = _scope(user)
        # ⚠️ `level_order` 는 **위험등급만** 담는다. 그대로 쓰면 노면 등급이
        #    전부 seq 0 이 되어, 아래 순서 검사가 값으로만 정렬한 뒤 자기
        #    자신과 비교하는 꼴이 된다 — **검사가 늘 통과한다.**
        #    도메인마다 제 성격의 순서를 쓴다.
        orders = {d: V.order_for_domain(db, d) for d in {r.domain for r in rows}}
        errs: list[str] = []
        staged: dict[int, float] = {}
        for r in rows:
            raw = form.get(f"min_{r.id}")
            if raw is None:
                continue
            if scope is not None and r.domain not in scope:
                errs.append(f"{r.domain} {r.level_code}: 담당하지 않는 "
                            "도메인의 구간은 고칠 수 없습니다.")
                continue
            try:
                v = float(str(raw).strip())
            except (TypeError, ValueError):
                errs.append(f"{r.domain} {r.level_code}: 숫자를 입력하세요.")
                continue
            if v < 0:
                errs.append(f"{r.domain} {r.level_code}: 음수는 넣을 수 없습니다.")
                continue
            staged[r.id] = v

        # ⚠️ 높은 등급의 문턱이 낮은 등급보다 작으면 **낮은 등급이 영원히 안
        # 나온다.** 저장 전에 막는다 — 저장하고 나서 알아차리면 그동안의
        # 판정이 전부 틀린다.
        by_domain: dict[str, list[tuple[int, float]]] = {}
        for r in rows:
            if r.id in staged:
                by_domain.setdefault(r.domain, []).append(
                    (orders.get(r.domain, {}).get(r.level_code, 0),
                     staged[r.id]))
        for domain, pairs in by_domain.items():
            pairs.sort()
            for (s1, v1), (s2, v2) in zip(pairs, pairs[1:]):
                if v2 <= v1:
                    errs.append(
                        f"{domain}: 높은 등급의 기준값이 낮은 등급보다 "
                        f"크거나 같아야 합니다({v1} → {v2}).")

        if errs:
            db.rollback()
            return _levels_page(request, db, user, errors=errs, status_code=400)

        n = 0
        for r in rows:
            if r.id in staged and r.min_value != staged[r.id]:
                r.min_value = staged[r.id]
                r.updated_by = user.login_id
                n += 1
        if n:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="위험등급 구간 변경",
                         after={"count": n})
        db.commit()
        return _levels_page(request, db, user, notice=(
            f"등급 구간 {n}건을 고쳤습니다. 다음 판정부터 반영됩니다."
            if n else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/flood-notify", response_class=HTMLResponse)
    def flood_notify_grade(request: Request, grade: str = Form("4"),
                           db: Session = Depends(get_db),
                           user: User = Depends(require_page(R.SETTINGS_OPS,
                                                             R.Action.EDIT))):
        """침수 위험도(RiskEngine) 독립 알림의 발동 등급 (2026-08-21 신설).

        ⚠️ 이 기본값(4)은 개발사 판단이라 실환경에서 검증되지 않았다 —
        여기서 관리자가 재난 담당부서 협의 결과로 조정한다.
        """
        scope = _scope(user)
        if scope is not None and "flood" not in scope:
            raise HTTPException(status_code=403,
                                detail="침수 담당자만 이 값을 고칠 수 있습니다.")
        try:
            n = int(grade)
        except (TypeError, ValueError):
            return _levels_page(request, db, user, status_code=400,
                                errors=["등급은 숫자(1~5)로 입력하십시오."])
        if not (1 <= n <= 5):
            return _levels_page(request, db, user, status_code=400,
                                errors=["등급은 1~5 사이여야 합니다."])
        before = ug_settings.flood_notify_min_grade(db)
        ug_settings.set_flood_notify_min_grade(db, n)
        if n != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="침수 위험도 알림 발동 등급",
                         before={"grade": before}, after={"grade": n})
        db.commit()
        return _levels_page(request, db, user, notice=(
            f"침수 위험도 알림 발동 등급을 {n}등급으로 저장했습니다. "
            "다음 판정부터 반영됩니다." if n != before else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/traffic-notify", response_class=HTMLResponse)
    def traffic_notify_severity(request: Request, severity: str = Form("2"),
                                db: Session = Depends(get_db),
                                user: User = Depends(require_page(R.SETTINGS_OPS,
                                                                  R.Action.EDIT))):
        """교통위험 SOLAPI 알림의 발동 심각도 (2026-08-26 신설)."""
        scope = _scope(user)
        if scope is not None and "traffic" not in scope:
            raise HTTPException(status_code=403,
                                detail="교통위험 담당자만 이 값을 고칠 수 있습니다.")
        try:
            n = int(severity)
        except (TypeError, ValueError):
            return _levels_page(request, db, user, status_code=400,
                                errors=["심각도는 숫자(0~3)로 입력하십시오."])
        if not (0 <= n <= 3):
            return _levels_page(request, db, user, status_code=400,
                                errors=["심각도는 0~3 사이여야 합니다."])
        before = ug_settings.traffic_notify_min_severity(db)
        ug_settings.set_traffic_notify_min_severity(db, n)
        if n != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="교통위험 알림 발동 심각도",
                         before={"severity": before}, after={"severity": n})
        db.commit()
        return _levels_page(request, db, user, notice=(
            f"교통위험 알림 발동 심각도를 저장했습니다. 다음 판정부터 반영됩니다."
            if n != before else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/crowd-notify", response_class=HTMLResponse)
    def crowd_notify_severity(request: Request, severity: str = Form("3"),
                              db: Session = Depends(get_db),
                              user: User = Depends(require_page(R.SETTINGS_OPS,
                                                                R.Action.EDIT))):
        """인파 밀집도 이벤트 발생 심각도 (2026-08-26 신설). 인파는 자동
        SOLAPI 알림이 없어, 이벤트 생성 문턱이 실질적인 경보 자리다."""
        scope = _scope(user)
        if scope is not None and "crowd" not in scope:
            raise HTTPException(status_code=403,
                                detail="인파 담당자만 이 값을 고칠 수 있습니다.")
        try:
            n = int(severity)
        except (TypeError, ValueError):
            return _levels_page(request, db, user, status_code=400,
                                errors=["심각도는 숫자(0~4)로 입력하십시오."])
        if not (0 <= n <= 4):
            return _levels_page(request, db, user, status_code=400,
                                errors=["심각도는 0~4 사이여야 합니다."])
        before = ug_settings.crowd_density_min_severity(db)
        ug_settings.set_crowd_density_min_severity(db, n)
        if n != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="인파 밀집도 이벤트 발생 심각도",
                         before={"severity": before}, after={"severity": n})
        db.commit()
        return _levels_page(request, db, user, notice=(
            "인파 밀집도 이벤트 발생 심각도를 저장했습니다. 다음 판정부터 반영됩니다."
            if n != before else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/road-notify", response_class=HTMLResponse)
    def road_notify_grade(request: Request, grade: str = Form("1"),
                          db: Session = Depends(get_db),
                          user: User = Depends(require_page(R.SETTINGS_OPS,
                                                            R.Action.EDIT))):
        """노면 손상 이벤트 발생 등급 (2026-08-26 신설). 노면도 자동 SOLAPI
        알림이 없다."""
        scope = _scope(user)
        if scope is not None and "road" not in scope:
            raise HTTPException(status_code=403,
                                detail="노면 담당자만 이 값을 고칠 수 있습니다.")
        try:
            n = int(grade)
        except (TypeError, ValueError):
            return _levels_page(request, db, user, status_code=400,
                                errors=["등급은 숫자(1~4)로 입력하십시오."])
        if not (1 <= n <= 4):
            return _levels_page(request, db, user, status_code=400,
                                errors=["등급은 1~4 사이여야 합니다."])
        before = ug_settings.road_event_min_grade(db)
        ug_settings.set_road_event_min_grade(db, n)
        if n != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="노면 손상 이벤트 발생 등급",
                         before={"grade": before}, after={"grade": n})
        db.commit()
        return _levels_page(request, db, user, notice=(
            "노면 손상 이벤트 발생 등급을 저장했습니다. 다음 분석부터 반영됩니다."
            if n != before else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/rainfall-backend", response_class=HTMLResponse)
    def rainfall_backend_save(request: Request, backend: str = Form("sine"),
                              db: Session = Depends(get_db),
                              user: User = Depends(require_page(R.SETTINGS_OPS,
                                                                R.Action.EDIT))):
        """강수(rainfall) 데이터 전역 출처 저장 (2026-08-27 신설).

        ⚠️ **시스템관리자 전용.** 침수·교통위험 두 도메인이 함께 쓰는 전역
        값이라 특정 부서 소관으로 나눌 수 없다(`/settings/flow/build`와
        같은 이유).
        """
        if user.role != R.Role.SYS.value:
            raise HTTPException(status_code=403,
                                detail="강수 데이터 출처는 시스템관리자만 고칠 수 있습니다.")
        if backend not in ug_settings.RAINFALL_BACKENDS:
            return _levels_page(request, db, user, status_code=400,
                                errors=["강수 출처 값이 올바르지 않습니다."])
        before = ug_settings.rainfall_backend(db)
        ug_settings.set_rainfall_backend(db, backend)
        if backend != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="강수 데이터 전역 출처 변경",
                         before={"backend": before}, after={"backend": backend})
        db.commit()
        warn = ""
        if backend == "kma" and not os.environ.get("KMA_SERVICE_KEY"):
            warn = (" ⚠ .env 의 KMA_SERVICE_KEY 가 비어 있어, 실제로는 지금도 "
                    "합성값(sine)으로 동작합니다.")
        return _levels_page(request, db, user, notice=(
            f"강수 데이터 출처를 저장했습니다. 카메라별로 따로 지정하지 않은 "
            f"지점부터 다음 재시작 시 반영됩니다.{warn}"
            if backend != before else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/crowd-continuous-source", response_class=HTMLResponse)
    def crowd_continuous_source_save(request: Request, source: str = Form("detector"),
                                     db: Session = Depends(get_db),
                                     user: User = Depends(require_page(
                                         R.SETTINGS_OPS, R.Action.EDIT))):
        """인파 상시 카메라별 모니터링의 전역 검출 소스 저장 (2026-08-27 신설).

        ⚠️ **시스템관리자 전용** — CPU 부담이 큰 기능을 카메라 전부에 걸쳐
        한 번에 되돌려야 할 수 있는 비상 전환 스위치라, 도메인 담당자
        (MGR)에게 나눠 주지 않는다(`rainfall-backend`와 같은 원칙).
        """
        if user.role != R.Role.SYS.value:
            raise HTTPException(status_code=403,
                                detail="인파 상시 검출 소스는 시스템관리자만 고칠 수 있습니다.")
        if source not in ug_settings.CROWD_CONTINUOUS_SOURCES:
            return _levels_page(request, db, user, status_code=400,
                                errors=["검출 소스 값이 올바르지 않습니다."])
        before = ug_settings.crowd_continuous_source(db)
        ug_settings.set_crowd_continuous_source(db, source)
        if source != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="인파 상시 검출 소스 전역 변경",
                         before={"source": before}, after={"source": source})
        db.commit()
        return _levels_page(request, db, user, notice=(
            "인파 상시 검출 소스를 저장했습니다. 카메라별로 따로 지정하지 않은 "
            "지점부터 다음 재시작 시 반영됩니다."
            if source != before else "바뀐 내용이 없습니다."))

    @app.post("/settings/levels/crowd-tile-grid", response_class=HTMLResponse)
    def crowd_tile_grid_save(request: Request, grid: str = Form("2x2"),
                             db: Session = Depends(get_db),
                             user: User = Depends(require_page(
                                 R.SETTINGS_OPS, R.Action.EDIT))):
        """인파 검출 타일 격자 전역 기본값 저장 (2026-08-29 신설,
        「4대탐지기능 성능개선 로드맵」 1단계 — 원경 인물 검출력 개선).

        ⚠️ **시스템관리자 전용** — crowd-continuous-source와 같은 이유
        (CPU 부담이 큰 전역 스위치를 도메인 담당자에게 나눠 주지 않는다).
        """
        if user.role != R.Role.SYS.value:
            raise HTTPException(status_code=403,
                                detail="인파 타일 격자는 시스템관리자만 고칠 수 있습니다.")
        if grid not in ug_settings.CROWD_TILE_GRID_OPTIONS:
            return _levels_page(request, db, user, status_code=400,
                                errors=["타일 격자 값이 올바르지 않습니다."])
        before = ug_settings.crowd_tile_grid(db)
        ug_settings.set_crowd_tile_grid(db, grid)
        if grid != before:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target="인파 타일 격자 전역 변경",
                         before={"grid": before}, after={"grid": grid})
        db.commit()
        return _levels_page(request, db, user, notice=(
            "인파 타일 격자를 저장했습니다. 카메라별로 따로 지정하지 않은 "
            "지점부터 다음 재시작 시 반영됩니다."
            if grid != before else "바뀐 내용이 없습니다."))

    # ---------- S-96 지점 방향 관리 ----------
    def _cam_domains(cam) -> set[str]:
        return {d for d, v in C.domain_map(cam).items() if v.get("enabled")}

    def _aim_page(request: Request, db: Session, user: User, *,
                  notice: str = "", errors: list[str] | None = None,
                  status_code: int = 200):
        cams = list(db.scalars(select(Camera).order_by(Camera.id)))
        # ⚠️ MGR 은 담당 도메인과 하나라도 겹치는 카메라만 본다·고친다
        # (2026-08-22 전수점검 — 방향각은 물리적 속성이라 카메라 단위로
        # "하나라도 겹치면" 기준을 쓴다, routes_cameras.cameras_update 와 동일).
        scope = _scope(user)
        if scope is not None:
            cams = [c for c in cams if _cam_domains(c) & scope]
        filled = sum(1 for c in cams if c.bearing_deg is not None)
        return templates.TemplateResponse(request, "settings_aim.html", {
            **base_ctx(request, user), "active": "set-aim",
            "cams": cams, "purposes": sorted(C.PURPOSE_LABELS.items()),
            "filled": filled, "total": len(cams),
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/settings/aim", response_class=HTMLResponse)
    def aim_page(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_page(R.SETTINGS_OPS,
                                                   R.Action.VIEW))):
        return _aim_page(request, db, user)

    @app.post("/settings/aim", response_class=HTMLResponse)
    async def aim_save(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_OPS,
                                                         R.Action.EDIT))):
        """전 지점 방향각을 한 화면에서 저장한다.

        ⚠️ **빈 칸은 「모른다」로 저장한다.** 0 으로 바꾸면 「북쪽을 본다」가
        되어 사각지대 분석이 통째로 거짓이 된다. 39지점을 하나씩 수정 폼으로
        여는 것이 현실적이지 않아 이 화면을 따로 뒀다.
        """
        form = await request.form()
        cams = list(db.scalars(select(Camera)))
        scope = _scope(user)
        if scope is not None:
            cams = [c for c in cams if _cam_domains(c) & scope]
        errs: list[str] = []
        n = 0
        for cam in cams:
            data = {}
            for key in ("bearing_deg", "tilt_deg", "fov_deg", "purpose"):
                field = f"{key}_{cam.id}"
                if field in form:
                    data[key] = (form.get(field) or "").strip()
            if not data:
                continue
            bad = C._validate_aim(data)
            if bad:
                errs += [f"{cam.name}: {m}" for m in bad]
                continue
            before = (cam.bearing_deg, cam.tilt_deg, cam.fov_deg, cam.purpose)
            C._apply_aim(cam, data)
            if (cam.bearing_deg, cam.tilt_deg, cam.fov_deg, cam.purpose) != before:
                n += 1
        if errs:
            db.rollback()
            return _aim_page(request, db, user, errors=errs, status_code=400)
        if n:
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request), target=f"지점 방향 {n}건 변경")
        db.commit()
        return _aim_page(request, db, user, notice=(
            f"{n}개 지점의 방향 정보를 저장했습니다."
            if n else "바뀐 내용이 없습니다."))

    # ---------- S-97 상·하류 관리 ----------
    def _flow_page(request: Request, db: Session, user: User, *,
                   notice: str = "", errors: list[str] | None = None,
                   status_code: int = 200):
        cams = list(db.scalars(select(Camera).order_by(Camera.id)))
        # ⚠️ MGR 은 담당 도메인과 하나라도 겹치는 카메라만 상·하류 지정
        # 대상으로 고를 수 있다(2026-08-22 전수점검).
        scope = _scope(user)
        if scope is not None:
            cams = [c for c in cams if _cam_domains(c) & scope]
        allowed_ids = {c.id for c in cams}
        pairs = REL.flow_pairs(db)
        if scope is not None:
            pairs = [p for p in pairs
                    if p["upper_id"] in allowed_ids or p["lower_id"] in allowed_ids]
        # 거리를 함께 보여 준다 — 멀리 떨어진 두 지점을 상·하류로 묶었으면
        # 잘못 고른 것일 가능성이 높다.
        by_id = {c.id: c for c in cams}
        for p in pairs:
            a, b = by_id.get(p["upper_id"]), by_id.get(p["lower_id"])
            if a and b and None not in (a.lat, a.lng, b.lat, b.lng):
                p["distance_m"] = int(round(
                    REL.haversine_m(a.lat, a.lng, b.lat, b.lng)))
            else:
                p["distance_m"] = None
        return templates.TemplateResponse(request, "settings_flow.html", {
            **base_ctx(request, user), "active": "set-flow",
            "cams": cams, "pairs": pairs,
            "adjacent_count": REL.adjacency_count(db),
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/settings/flow", response_class=HTMLResponse)
    def flow_page(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_page(R.SETTINGS_OPS,
                                                    R.Action.VIEW))):
        return _flow_page(request, db, user)

    @app.post("/settings/flow/set", response_class=HTMLResponse)
    def flow_set(request: Request, upper_id: str = Form(""),
                 lower_id: str = Form(""), remove: str = Form(""),
                 db: Session = Depends(get_db),
                 user: User = Depends(require_page(R.SETTINGS_OPS,
                                                   R.Action.EDIT))):
        upper, lower = upper_id.strip(), lower_id.strip()
        if not upper or not lower:
            return _flow_page(request, db, user, status_code=400,
                              errors=["상류와 하류 지점을 모두 고르세요."])
        # ⚠️ 두 카메라 모두에 담당 도메인이 하나라도 걸쳐 있어야 한다
        # (2026-08-22 전수점검, routes_cameras.cameras_links_flow 와 동일).
        scope = _scope(user)
        if scope is not None:
            cam_u, cam_l = C.get(db, upper), C.get(db, lower)
            if ((cam_u is not None and not (_cam_domains(cam_u) & scope))
                    or (cam_l is not None and not (_cam_domains(cam_l) & scope))):
                raise HTTPException(status_code=403,
                                    detail="이 지점들에 대한 권한이 없습니다.")
        if remove:
            n = REL.clear_flow(db, upper, lower)
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request),
                         target=f"상·하류 해제 {upper}→{lower}")
            db.commit()
            return _flow_page(request, db, user, notice=(
                f"상·하류 지정을 해제했습니다({n}건)."
                if n else "지정된 관계가 없었습니다."))

        errs = REL.set_flow(db, upper, lower)
        if errs:
            return _flow_page(request, db, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"상·하류 지정 {upper}→{lower}")
        db.commit()
        return _flow_page(request, db, user, notice=(
            "상·하류를 지정했습니다. 상류에서 「경계」 이상 사건이 열리면 "
            "하류에 선행 경고가 뜹니다."))

    @app.post("/settings/flow/build", response_class=HTMLResponse)
    def flow_build(request: Request, radius_m: str = Form("500"),
                   preview: str = Form(""),
                   db: Session = Depends(get_db),
                   user: User = Depends(require_page(R.SETTINGS_OPS,
                                                     R.Action.EDIT))):
        # ⚠️ 시스템관리자 전용 — 전 지점 좌표를 대상으로 한 전역 재계산이라
        # 도메인 하나로 특정할 수 없다(2026-08-22 전수점검).
        if user.role != R.Role.SYS.value:
            raise HTTPException(status_code=403,
                                detail="이 작업은 시스템관리자만 할 수 있습니다.")
        try:
            radius = int((radius_m or "").strip())
        except (TypeError, ValueError):
            radius = -1
        if not (50 <= radius <= 5000):
            return _flow_page(request, db, user, status_code=400,
                              errors=["인접 판정 반경은 50~5000m 사이여야 합니다."])
        dry = bool(preview)
        got = REL.build_adjacency(db, radius_m=radius, dry_run=dry)
        warn = ""
        if got["zero_distance_pairs"]:
            warn = (f" ⚠ 좌표가 완전히 같은 지점 쌍이 "
                    f"{got['zero_distance_pairs']}건 있습니다 — 데이터 확인이 "
                    "필요합니다.")
        if dry:
            return _flow_page(request, db, user, notice=(
                f"[미리보기] 반경 {radius}m 기준 인접 관계 {got['links']}건이 "
                f"만들어집니다. 저장하지 않았습니다.{warn}"))
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="CCTV 인접 관계 재생성",
                     after={"radius_m": radius, "links": got["links"]})
        db.commit()
        return _flow_page(request, db, user, notice=(
            f"인접 관계 {got['links']}건을 만들었습니다"
            f"(이전 자동 생성분 {got['removed']}건 교체). "
            f"사람이 지정한 상·하류는 그대로입니다.{warn}"))
