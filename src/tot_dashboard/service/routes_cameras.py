"""S-80 CCTV 관리 · S-81 도메인별 ROI 설정.

관리자 작업 흐름은 세 단계다.
  ① CCTV 등록
  ② **탐지서비스별로 탐지할 CCTV 선택** (`camera_domains.enabled`)
  ③ **선택된 것 중 상시 탐지 지정** (`camera_domains.continuous`)

상시가 아닌 것은 각 도메인의 탐지 화면에서 카메라를 골라 실행한다.
"""
from __future__ import annotations

import json
import logging
import urllib.parse

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..core import audit
from ..core import calibration
from ..core import camera_bulk as BULK
from ..core import cameras as C
from ..core import cctv_sources as SRC
from ..core import config_rev
from ..core import config_rev_bridge
from ..core import regions as RG
from ..core import relations as REL
from ..core import restream as RESTREAM
from ..core import roles as R
from ..core import settings as ug_settings
from ..core import snapshot as SNAP
from ..core import topis_sources as TOPIS
from ..core import video_quality as VQ
from ..core import video_store as VID
from ..core.auth import client_ip, get_db, require_page
from ..core.models import User

XLSX_TYPE = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

log = logging.getLogger("urbanguard.cameras")

DOMAIN_ORDER = [R.Domain.FLOOD, R.Domain.TRAFFIC, R.Domain.CROWD, R.Domain.ROAD]
# 네 탐지서비스 모두 지점별로 상시 탐지를 켜고 끌 수 있다.
# 도메인마다 부하가 달라 주기가 다르다(service/continuous.py) — 침수는 초 단위
# 연속 분석, 인파는 수 초 간격 표본, 노면은 분 단위 순회다. 관리자가 켠 지점
# 수만큼 CPU를 쓰므로, 지점을 늘릴 때는 서버 부하를 함께 확인해야 한다.
CONTINUOUS_CAPABLE = {d.value for d in DOMAIN_ORDER}


def _scope(user: User) -> set[str] | None:
    """MGR 만 담당 도메인으로 제한된다. SYS 는 전 도메인(``None``).

    ``routes_events.py``/``routes_notify.py`` 의 ``_scope()`` 와 같은 패턴이다
    (``core/roles.py`` 의 ``SETTINGS_OPS`` 가 ``DOMAIN_SCOPED`` 로 선언돼
    있음에도, 이 파일의 모든 라우트가 ``domain=`` 을 안 넘겨 실제로는 전혀
    걸러지지 않던 문제를 2026-08-22 전수점검에서 발견해 고친다).
    """
    if user.role == R.Role.MGR.value:
        return user.domain_set
    return None


def _cam_domains(cam) -> set[str]:
    """카메라가 실제로(사용 지정) 걸쳐 있는 도메인 집합."""
    from ..core import cameras as _C  # 지연 임포트로 순환참조 회피
    return {d for d, v in _C.domain_map(cam).items() if v.get("enabled")}


def register(app, templates, base_ctx) -> None:
    # ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 예전에는 ``store``·``runner``
    # 를 받았다. ``store``는 ROI 편집기 정지영상 미리보기의 "파이프라인이
    # 돌고 있으면 그 프레임이 가장 싸다"는 최적화용이었는데, 침수·교통이
    # 별도 프로세스가 되며 platform-shell엔 그 store가 없다 — 이미 있던
    # 폴백(``SNAP.grab(cam)``)만 쓰도록 단순화했다(설계 검토에서 "그냥
    # 폴백만 쓰자"로 결론). ``runner``는 통행 방향 자동 생성("자동 생성"
    # 버튼, ``PipelineRunner.traffic_flow_samples``)에만 쓰였는데, 그
    # 기능 자체가 실행 중인 교통 추적기 상태가 있어야 답할 수 있어
    # traffic-service의 ``/api/traffic/roi-flow/propose``로 옮겼다(이
    # URL은 `/settings/cameras/*` 접두사에 안 걸려 있어야 해서다).

    def _require_domain(user: User, domain: str) -> None:
        """단일 도메인(ROI·캘리브레이션처럼 도메인이 명시적인 요청)에 대한
        담당 여부를 확인한다. MGR 이 아니면(SYS) 항상 통과."""
        scope = _scope(user)
        if scope is not None and domain not in scope:
            label = R.DOMAIN_LABELS.get(R.Domain(domain), domain)
            raise HTTPException(status_code=403,
                                detail=f"「{label}」 도메인에 대한 권한이 없습니다.")

    def _require_any_domain(user: User, domains: set[str]) -> None:
        """이 카메라가 걸친 도메인 중 **하나라도** 담당이면 통과 —
        메타데이터 수정처럼 여러 도메인이 얽힌 자원에 쓴다."""
        scope = _scope(user)
        if scope is not None and not (domains & scope):
            raise HTTPException(status_code=403,
                                detail="이 카메라에 대한 권한이 없습니다"
                                       "(담당 도메인이 아닙니다).")

    def _require_all_domains(user: User, domains: set[str]) -> None:
        """이 카메라가 걸친 도메인 **전부**가 담당이어야 통과 — 삭제처럼
        되돌릴 수 없는 조작에 쓴다(다른 부서가 같이 쓰는 카메라를 혼자
        지우지 못하게)."""
        scope = _scope(user)
        if scope is not None and not (domains <= scope):
            raise HTTPException(status_code=403,
                                detail="이 카메라는 다른 부서도 함께 쓰고 있어 "
                                       "삭제할 수 없습니다(담당 도메인 전체가 "
                                       "일치해야 합니다).")

    def _require_sys(user: User) -> None:
        """부서·지역을 가리지 않는 전역 일괄 작업 — 시스템관리자 전용으로
        좁힌다(예전에는 MGR 도 전혀 걸러지지 않고 다 됐다)."""
        if user.role != R.Role.SYS.value:
            raise HTTPException(status_code=403,
                                detail="이 작업은 시스템관리자만 할 수 있습니다.")

    def _list_page(request: Request, db: Session, user: User, *,
                   notice: str = "", errors: list[str] | None = None,
                   preview: list[dict] | None = None,
                   preview_kind: str = "", preview_payload: str = "",
                   status_code: int = 200):
        cams = C.list_all(db)
        # ⚠️ MGR 은 담당 도메인과 하나라도 겹치는 카메라만 본다 — 그동안
        #   전혀 걸러지지 않아 인파 담당자가 침수·교통·도로 카메라까지 다
        #   보고 고칠 수 있었다(2026-08-22 전수점검에서 발견해 고침).
        scope = _scope(user)
        if scope is not None:
            cams = [c for c in cams if _cam_domains(c) & scope]
        roi = C.roi_status(db, cams)

        # 지역 선택. **저장 뒤 다시 그릴 때도 유지된다** — POST 처리부가 전부
        # 이 함수를 부르므로, 요청 쿼리에서 한 번만 읽는다. 폼에 hidden 으로
        # 실어 나르는 방식은 폼이 늘 때마다 빠뜨리기 쉽다.
        q = request.query_params
        sel_sido = (q.get("region") or "").strip()
        sel_gu = (q.get("sigungu") or "").strip()

        rows = []
        for cam in cams:
            # 지점별 캘리브레이션 — 화면 단위를 실제 물리 단위로 바꾸는 값.
            # 이게 있어야 국내외 기준(명/㎡·침수심 cm·구간 단위)과 연결된다.
            cal = {}
            for d in DOMAIN_ORDER:
                c = calibration.of(cam, d.value)
                cal[d.value] = {
                    "ground": c.ground is not None,
                    "depth": c.depth is not None,
                    "section": c.section.length_m if c.section else None,
                    "raw": c.to_dict(),
                }
            # 상시 화질 감시(2026-08-30) — 스캐너(core/video_quality.py)가
            # 재배포 경로를 저빈도로 재서 매긴 등급을 그대로 뿌린다.
            # model_stats()의 {state, state_badge} 패턴과 동일하게 파이썬
            # 에서 라벨·배지를 미리 만들어 템플릿은 뿌리기만 한다.
            grade = VQ.grade_for(cam.id)
            quality = {
                "on": {"label": "정상", "badge": "on"},
                "warn": {"label": "주의", "badge": "warn"},
                "crit": {"label": "손상 심각", "badge": "crit"},
                "unknown": {"label": "연결 실패", "badge": "warn"},
            }.get(grade, {"label": "미측정", "badge": "off"})
            rows.append({
                "cam": cam,
                "domains": C.domain_map(cam),
                "roi": roi.get(cam.id, {}),
                "calibration": cal,
                "region": C.region_label_of(cam),
                "quality": quality,
            })
        # 지역별 등록 현황 — 어느 지역에 몇 대가 있는지 보이지 않으면
        # 지역 단위로 해지할 판단을 할 수 없다. **거르기 전 전체**로 센다.
        region_groups = [{"key": g["key"], "label": g["label"],
                          "count": g["count"], "sigungu": g["sigungu"]}
                         for g in C.group_by_region(cams)]

        # 도메인별 사용 현황 — "등록된 CCTV 00개소"만으로는 그 카메라들이
        # 실제로 4개 탐지서비스 중 어디에 쓰이고 있는지 알 수 없다.
        # **지역 필터와 무관하게 전체 기준**으로 센다(region_groups와
        # 같은 이유 — 특정 지역만 보고 있어도 시스템 전체 사용 현황은
        # 그대로 보여야 판단할 수 있다). `rows`가 아래에서 지역별로
        # 걸러지기 **전**에 계산해야 한다.
        domain_stats = []
        for d in DOMAIN_ORDER:
            dv = d.value
            enabled = sum(1 for r in rows if r["domains"][dv]["enabled"])
            continuous = sum(1 for r in rows
                             if r["domains"][dv]["enabled"] and r["domains"][dv]["continuous"])
            domain_stats.append({
                "value": dv, "label": R.DOMAIN_LABELS[d],
                "enabled": enabled, "continuous": continuous,
                "selective": enabled - continuous,
            })

        # 화질 현황 요약 — "CCTV 관리" 화면을 열기만 해도 전체 손상
        # 현황이 한눈에 보이게 한다(2026-08-30, 상시 화질 감시 4단계).
        # domain_stats와 같은 이유로 지역 필터 전 **전체 기준**으로 센다.
        quality_counts = {"on": 0, "warn": 0, "crit": 0, "off": 0}
        for r in rows:
            quality_counts[r["quality"]["badge"]] = quality_counts.get(r["quality"]["badge"], 0) + 1
        quality_stats = {
            "on": quality_counts["on"], "warn": quality_counts["warn"],
            "crit": quality_counts["crit"], "unmeasured": quality_counts["off"],
            "enabled": ug_settings.video_quality_enabled(db),
        }

        # 선택한 지역만 목록에 남긴다. 「지역 미상」은 region=none 으로 고른다 —
        # 빈 값은 「전체」와 구분되지 않는다.
        total_rows = len(rows)
        if sel_sido:
            want = "" if sel_sido == "none" else sel_sido
            rows = [r for r in rows if (C.region_of(r["cam"]) or "") == want]
            if sel_gu:
                want_gu = "" if sel_gu == "none" else sel_gu
                rows = [r for r in rows
                        if (r["cam"].sigungu or "").strip() == want_gu]
        return templates.TemplateResponse(request, "cameras.html", {
            **base_ctx(request, user), "active": "set-cameras",
            "rows": rows,
            "domain_order": [(d.value, R.DOMAIN_LABELS[d]) for d in DOMAIN_ORDER],
            "source_types": sorted(C.SOURCE_TYPES.items()),
            "purposes": sorted(C.PURPOSE_LABELS.items()),
            "flow_pairs": REL.flow_pairs(db),
            "roi_shapes": C.ROI_SHAPES,
            "can_edit": R.can(user.role, R.SETTINGS_OPS, R.Action.EDIT),
            # 일괄 등록 미리보기 — 확인 전에는 DB에 넣지 않는다.
            "preview": preview or [],
            "preview_kind": preview_kind,
            "preview_payload": preview_payload,
            "preview_summary": BULK.summarize(preview) if preview else None,
            "regions": [(k, label) for k, (label, _bbox) in SRC.REGIONS.items()],
            "region_groups": region_groups,
            "domain_stats": domain_stats,
            "quality_stats": quality_stats,
            "sel_sido": sel_sido, "sel_gu": sel_gu,
            "total_rows": total_rows,
            # 「구·군 미지정」을 고른 것과 「시/도 전체」를 고른 것이 같은 이름으로
            # 보이면, 수가 다른 이유를 알 수 없다.
            "sel_label": ("전체" if not sel_sido
                          else RG.label_of(None, None) if sel_sido == "none"
                          else RG.label_of(sel_sido, "") + " · 구·군 미지정"
                          if sel_gu == "none"
                          else RG.label_of(sel_sido, sel_gu)),
            # 등록·수정 폼의 시/도·구군 선택상자. 화면과 서버가 같은 목록을
            # 봐야 저장 단계에서만 거부되는 일이 없다.
            "sido_choices": RG.sido_choices(),
            "sigungu_map": RG.SIGUNGU,
            "has_api_key": bool(SRC.api_key()),
            "notice": notice, "errors": errors or [],
        }, status_code=status_code)

    @app.get("/settings/cameras", response_class=HTMLResponse)
    def cameras_page(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_page(R.SETTINGS_OPS,
                                                       R.Action.VIEW))):
        return _list_page(request, db, user)

    def _selections(form) -> dict[str, dict]:
        """폼에서 도메인별 사용·상시 여부를 읽는다."""
        out = {}
        for d in DOMAIN_ORDER:
            enabled = form.get(f"use_{d.value}") is not None
            cont = (form.get(f"cont_{d.value}") is not None
                    and d.value in CONTINUOUS_CAPABLE)
            out[d.value] = {"enabled": enabled, "continuous": cont}
        return out

    def _calibration_from_form(form, domain: str) -> tuple[dict, list[str]]:
        """캘리브레이션 폼을 읽는다. (저장할 값, 형식 오류 목록)

        좌표처럼 값이 많은 항목은 JSON 으로 받습니다. 네 점을 화면에서 찍는
        편집기가 있으면 더 좋겠지만, 그것이 없다고 기능 자체를 미룰 이유는
        없습니다 — 값은 현장에서 한 번 재면 바뀌지 않습니다.
        """
        data: dict = {}
        errors: list[str] = []

        def _json(field: str, label: str):
            raw = (form.get(field) or "").strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except Exception:  # noqa: BLE001
                errors.append(f"{label}: JSON 형식이 아닙니다. 예: [[0,0],[100,0],"
                              f"[100,100],[0,100]]")
                return None

        # 지면 평면 — 인파(명/㎡)·침수(픽셀 속도 보정)·교통(km/h 실측)에 쓰인다.
        # ⚠️ 2026-08-26 — "traffic" 추가(docs/202608210801 9절 확인사항 1번이
        #    남겨 둔 자리). 그전까지 화면의 km/h는 `block.get("calib")` 를
        #    읽었는데 그 키를 채우는 코드가 없어 항상 가짜 고정계수(mpp=0.06)
        #    였다(`docs/202608260842/` 계획 Phase 2 — 결함 시정).
        if domain in ("crowd", "flood", "traffic"):
            ip = _json("ground_image_points", "지면 보정 화면 좌표")
            wp = _json("ground_world_points", "지면 보정 실제 좌표")
            if ip is not None and wp is not None:
                data["ground"] = {"image_points": ip, "world_points": wp}

        # 침수심 대응표 — 침수 전용.
        if domain == "flood":
            pts = _json("depth_points", "침수심 대응표")
            if pts is not None:
                data["depth"] = {"points": pts}

        # 구간 길이 — 노면 전용.
        if domain == "road":
            raw = (form.get("section_length_m") or "").strip()
            if raw:
                try:
                    data["section"] = {"length_m": float(raw)}
                except ValueError:
                    errors.append("구간 길이: 숫자를 입력하십시오.")

        return data, errors

    @app.post("/settings/cameras/create", response_class=HTMLResponse)
    async def cameras_create(request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_page(R.SETTINGS_OPS,
                                                               R.Action.EDIT))):
        form = await request.form()
        data = {k: (v if isinstance(v, str) else "") for k, v in form.items()}

        # ⚠️ MGR 이 담당하지 않는 도메인으로 새 카메라를 등록하지 못하게 한다
        # (2026-08-22 전수점검 — 예전에는 도메인 선택에 아무 제약이 없었다).
        sel = _selections(form)
        wanted = {d for d, v in sel.items() if v["enabled"]}
        scope = _scope(user)
        if scope is not None and not (wanted <= scope):
            outside = ", ".join(R.DOMAIN_LABELS[R.Domain(d)]
                                for d in wanted - scope)
            return _list_page(request, db, user, status_code=403, errors=[
                f"담당하지 않는 도메인({outside})은 사용으로 지정할 수 없습니다."])

        # 동영상을 올렸으면 경로를 손으로 적을 필요가 없다.
        # ⚠️ 임시로 받아 두고 **등록이 성공한 뒤에만** 확정한다 — 검증에서
        # 걸렸는데 파일만 남으면 DB와 디스크가 어긋난다.
        upload = form.get("video_file")
        staged = None
        if getattr(upload, "filename", ""):
            try:
                staged = await VID.stage_upload(upload, data.get("id", ""))
                data["source_type"] = "video"
                data["source_path"] = f"{VID.REL_PREFIX}/{staged.with_suffix('').name}"
            except VID.VideoError as e:
                return _list_page(request, db, user, errors=[str(e)],
                                  status_code=400)

        cam, errs = C.create(db, data)
        if errs:
            VID.discard(staged)
            return _list_page(request, db, user, errors=errs, status_code=400)
        if staged is not None:
            cam.source_path = VID.finalize(staged, cam.id)
        C.set_domains(db, cam, _selections(form))
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"CCTV 등록 {cam.id}",
                     after={"name": cam.name, "source": cam.source_type})
        db.commit()
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
        # ★ 2026-08-28 — CCTV 재배포 허브(MediaMTX) 경로 즉시 동기화.
        # `restream.add_path()`는 재배포가 꺼져 있거나 MediaMTX가 응답하지
        # 않아도 실패를 삼킨다 — 카메라 등록 자체를 절대 막지 않는다.
        if cam.source_type == "hls":
            RESTREAM.add_path(cam.id, cam.source_url, db)
        return _list_page(request, db, user, notice=(
            f"{cam.name}({cam.id}) 을 등록했습니다. "
            "침수·교통위험·노면 상시 탐지는 곧바로 반영되며, ROI 설정이 필요합니다. "
            "⚠ 인파 상시 탐지는 서비스 재시작 후 반영됩니다."))

    @app.post("/settings/cameras/{camera_id}/update", response_class=HTMLResponse)
    async def cameras_update(camera_id: str, request: Request,
                             db: Session = Depends(get_db),
                             user: User = Depends(require_page(R.SETTINGS_OPS,
                                                               R.Action.EDIT))):
        before = C.get(db, camera_id)
        if before is None:
            return _list_page(request, db, user, errors=["카메라를 찾을 수 없습니다."],
                              status_code=404)
        # ⚠️ 담당 도메인 중 하나라도 겹쳐야 메타데이터를 고칠 수 있다
        # (2026-08-22 전수점검 — 예전에는 이 확인 자체가 없었다).
        _require_any_domain(user, _cam_domains(before))

        form = await request.form()
        data = {k: (v if isinstance(v, str) else "") for k, v in form.items()}
        upload = form.get("video_file")
        staged = None
        if getattr(upload, "filename", ""):
            try:
                staged = await VID.stage_upload(upload, camera_id)
                data["source_type"] = "video"
                data["source_path"] = f"{VID.REL_PREFIX}/{staged.with_suffix('').name}"
            except VID.VideoError as e:
                return _list_page(request, db, user, errors=[str(e)],
                                  status_code=400)
        before_name = before.name
        cam, errs = C.update(db, camera_id, data)
        if errs:
            VID.discard(staged)
            return _list_page(request, db, user, errors=errs, status_code=400)
        if staged is not None:
            cam.source_path = VID.finalize(staged, camera_id)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"CCTV 수정 {camera_id}",
                     before={"name": before_name}, after={"name": cam.name})
        db.commit()
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
        # ★ 2026-08-28 — URL·소스 타입이 바뀌었을 수 있으니 매번 재동기화한다
        # (멱등이라 안 바뀌었어도 무해). hls가 아니게 바뀐 경우는 경로를 지운다.
        if cam.source_type == "hls":
            RESTREAM.add_path(cam.id, cam.source_url, db)
        else:
            RESTREAM.remove_path(cam.id, db)
        return _list_page(request, db, user,
                          notice=f"{cam.name} 정보를 수정했습니다.")

    @app.post("/settings/cameras/{camera_id}/domains", response_class=HTMLResponse)
    async def cameras_domains(camera_id: str, request: Request,
                              db: Session = Depends(get_db),
                              user: User = Depends(require_page(R.SETTINGS_OPS,
                                                                R.Action.EDIT))):
        cam = C.get(db, camera_id)
        if cam is None:
            return _list_page(request, db, user, errors=["카메라를 찾을 수 없습니다."],
                              status_code=404)
        form = await request.form()
        sel = _selections(form)
        prev = C.domain_map(cam)
        before = {d: v["enabled"] for d, v in prev.items()}
        # ⚠️ 실제로 값이 "바뀌는" 도메인만 담당 여부를 확인한다 — 안 바뀌는
        # 도메인은 화면에 같이 떠 있을 뿐이라, 그것까지 막으면 여러 부서가
        # 공유하는 카메라의 폼을 아무도 못 낸다(2026-08-22 전수점검에서 고침).
        changed = {d for d in R.DOMAIN_SHORT
                   if (prev.get(d, {}).get("enabled"), prev.get(d, {}).get("continuous"))
                   != (sel.get(d, {}).get("enabled"), sel.get(d, {}).get("continuous"))}
        scope = _scope(user)
        if scope is not None and not (changed <= scope):
            outside = ", ".join(R.DOMAIN_LABELS[R.Domain(d)]
                                for d in changed - scope)
            return _list_page(request, db, user, status_code=403, errors=[
                f"담당하지 않는 도메인({outside})의 탐지 지정은 바꿀 수 없습니다."])
        # 끄는 도메인에 ROI 가 있으면 **지워지지 않는다는 사실**을 알려 준다.
        # 안 알려 주면 화면에서 사라진 것을 보고 다시 그리게 된다.
        kept = [R.DOMAIN_LABELS[R.Domain(d)] for d in before
                if before[d] and not sel.get(d, {}).get("enabled")
                and C.has_roi(cam, d)]
        C.set_domains(db, cam, sel)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"CCTV 탐지 대상 변경 {camera_id}",
                     before=before,
                     after={d: f"{'사용' if v['enabled'] else '미사용'}"
                                f"{'/상시' if v['continuous'] else ''}"
                            for d, v in sel.items()})
        db.commit()
        # 상시 탐지 워처가 자고 있어도 곧바로 깨어나 목록을 다시 읽는다.
        # 이 신호가 없으면 화면에는 「상시」로 뜨는데 실제로는 아무도 그 지점을
        # 보지 않는 상태가 최대 15분(노면 주기) 이어진다.
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
        msg = f"{cam.name} 의 탐지 대상을 변경했습니다."
        if sel.get("flood", {}).get("continuous"):
            # 2026-08-22 — PipelineRunner 가 config_rev 를 구독해 재기동 없이
            # 워커를 띄우고 내린다(dynamic 재구성).
            msg += " 침수 상시 탐지에 곧바로 반영됩니다."
        if sel.get("traffic", {}).get("continuous"):
            # ⚠️ 2026-08-23 — 예전에는 이 메시지 자체가 없었다(교통위험은
            # 언급조차 안 됨). 실제로는 상시 처리 루프의 대상 목록이 침수만
            # 봐서, 침수를 미사용으로 둔 카메라는 교통위험을 상시로 켜도
            # 파이프라인에 아예 안 올라갔다 — 실사용 중 발견. 대상 목록을
            # 침수·교통위험 합집합으로 고친 뒤에야 이 문구가 사실이 됐다.
            msg += " 교통위험 상시 탐지에 곧바로 반영됩니다."
        if sel.get("road", {}).get("continuous"):
            msg += " 노면 상시 탐지에 곧바로 반영됩니다."
        if sel.get("crowd", {}).get("continuous"):
            msg += " ⚠ 인파 상시 탐지는 서비스 재시작 후 반영됩니다(추적 상태 보존)."
        if kept:
            msg += (f" {' · '.join(kept)} 의 ROI 는 지워지지 않고 보관됩니다 — "
                    "다시 「사용」으로 바꾸면 그대로 쓰입니다.")
        return _list_page(request, db, user, notice=msg)

    @app.post("/settings/cameras/{camera_id}/calibration", response_class=HTMLResponse)
    async def cameras_calibration(camera_id: str, request: Request,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(require_page(R.SETTINGS_OPS,
                                                                    R.Action.EDIT))):
        """지점별 캘리브레이션 저장 — 화면 단위를 실제 물리 단위로 바꾸는 값.

        이게 있어야 국내외 기준(명/㎡·침수심 cm·구간 단위)과 연결됩니다.
        보정 전에는 화면이 「미보정」으로 표시되며, **보정한 척하지 않습니다.**
        """
        cam = C.get(db, camera_id)
        if cam is None:
            return _list_page(request, db, user, errors=["카메라를 찾을 수 없습니다."],
                              status_code=404)
        form = await request.form()
        domain = str(form.get("domain") or "")
        # ★ 2026-08-21: 도메인 목록을 리터럴로 박아 두면 새 도메인을 추가할 때
        #   여기를 잊고 400 만 돌려주게 된다(실제로 그럴 뻔했다). enum 을
        #   그대로 쓴다.
        if domain not in {d.value for d in R.Domain}:
            return _list_page(request, db, user, errors=["알 수 없는 탐지서비스입니다."],
                              status_code=400)
        _require_domain(user, domain)

        data, parse_errors = _calibration_from_form(form, domain)
        if parse_errors:
            return _list_page(request, db, user, errors=parse_errors, status_code=400)

        errors = calibration.save(db, cam, domain, data)
        if errors:
            return _list_page(request, db, user, errors=errors, status_code=400)

        audit.record_and_commit(
            db, action=audit.SETTINGS_UPDATE, user=user, ip=client_ip(request),
            target=f"캘리브레이션 {camera_id}/{domain}",
            after={k: str(v)[:120] for k, v in data.items()})
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다

        cal = calibration.from_dict(data)
        done = []
        if cal.ground:
            # ★ 2026-08-26 — 지면 평면은 도메인마다 쓰임이 다르다(인파 명/㎡ ·
            #   침수 픽셀속도 보정 · 교통 km/h 실측, routes_cameras.py:232
            #   주석). 문구를 하나로 고정하면 교통 관리자가 저장했는데
            #   "명/㎡ 산출 가능"이라는 인파 문구를 보게 된다.
            _ground_label = {
                R.Domain.CROWD.value: "지면 평면(명/㎡ 산출 가능)",
                R.Domain.TRAFFIC.value: "지면 평면(km/h 실측 가능)",
            }.get(domain, "지면 평면(픽셀 속도 보정 가능)")
            done.append(_ground_label)
        if cal.depth:
            done.append("침수심 대응표(cm 추정 가능)")
        if cal.section:
            done.append("구간 길이(건/100m 산출 가능)")
        msg = (f"{cam.name} · {domain} 캘리브레이션을 저장했습니다 — "
               + ", ".join(done) if done
               else f"{cam.name} · {domain} 캘리브레이션을 지웠습니다.")
        return _list_page(request, db, user, notice=msg)

    # ⚠️ **아래 `{camera_id}/delete` 보다 먼저 등록해야 한다.** 뒤에 두면
    # `/settings/cameras/region/delete` 가 camera_id="region" 으로 잡혀
    # 「카메라를 찾을 수 없습니다」로 끝난다. 실제로 그렇게 새어 나갔다.
    @app.post("/settings/cameras/region/delete", response_class=HTMLResponse)
    def cameras_region_delete(request: Request, region: str = Form(""),
                              confirm: str = Form(""),
                              db: Session = Depends(get_db),
                              user: User = Depends(require_page(R.SETTINGS_OPS,
                                                                R.Action.EDIT))):
        """지역 단위 일괄 해지.

        되돌릴 수 없으므로 **지역 이름을 그대로 입력받아 확인**한다. 버튼 하나로
        수십 대가 사라지면 실수 한 번의 대가가 너무 크다.

        ⚠️ **시스템관리자 전용.** 지역 단위 일괄 해지는 도메인 하나로 특정할
        수 없는 전역 작업이라(그 지역의 카메라가 여러 부서에 걸쳐 있을 수
        있음), 부서담당자(MGR)에게는 개별 카메라 단위 조작만 허용한다
        (2026-08-22 전수점검 — 예전에는 아무 제약 없이 다 됐다).
        """
        _require_sys(user)
        label = SRC.region_label(region or None)
        if confirm.strip() != label:
            return _list_page(request, db, user, status_code=400, errors=[
                f"확인을 위해 「{label}」 을 그대로 입력해야 해지됩니다."])
        targets = C.by_region(db, region)
        names = [c.name for c in targets][:20]
        count, errs = C.delete_region(db, region)
        if errs:
            return _list_page(request, db, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request),
                     target=f"CCTV 지역 해지 {label} {count}대",
                     before={"region": region, "count": count, "names": names})
        db.commit()
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
        # ★ 2026-08-28 — 해지된 카메라들의 MediaMTX 경로도 함께 지운다.
        # `targets`는 삭제 전에 미리 잡아 둔 목록이다(위 510행).
        for c in targets:
            RESTREAM.remove_path(c.id, db)
        return _list_page(request, db, user, notice=(
            f"{label} 지점 {count}대를 해지했습니다. "
            "관련 ROI·캘리브레이션 설정도 함께 지워졌습니다."))

    # ⚠️ `{camera_id}/delete` 보다 **위에** 둬야 한다 — 아래에 두면
    # `/settings/cameras/links/build` 가 camera_id="links" 로 잡힌다.
    # 지역 해지 라우트에서 실제로 겪은 문제다(바로 위 주석 참고).
    @app.post("/settings/cameras/links/build", response_class=HTMLResponse)
    def cameras_links_build(request: Request, radius_m: str = Form("500"),
                            preview: str = Form(""),
                            db: Session = Depends(get_db),
                            user: User = Depends(require_page(R.SETTINGS_OPS,
                                                              R.Action.EDIT))):
        """좌표로 인접 관계를 다시 만든다 (관계 모델 1단계).

        ⚠️ **`auto=True` 인 인접 링크만 지우고 다시 만든다.** 사람이 지정한
        상·하류는 좌표로 복원할 수 없어서 건드리지 않는다.

        `preview` 가 오면 **세기만 하고 쓰지 않는다** — 반경을 바꿔 볼 때 쓴다.

        ⚠️ **시스템관리자 전용.** 전 지점 좌표를 대상으로 한 전역 재계산이라
        도메인 하나로 특정할 수 없다(2026-08-22 전수점검).
        """
        _require_sys(user)
        try:
            radius = int((radius_m or "").strip())
        except (TypeError, ValueError):
            radius = -1
        if not (50 <= radius <= 5000):
            return _list_page(request, db, user, status_code=400, errors=[
                "인접 판정 반경은 50~5000m 사이여야 합니다."])

        dry = bool(preview)
        got = REL.build_adjacency(db, radius_m=radius, dry_run=dry)
        # 좌표가 같은 지점이 실제로 있다(범내골교차로·광복로). 조용히 묶으면
        # 서로 다른 지점이 한 덩어리가 되므로 **반드시 드러낸다.**
        warn = ""
        if got["zero_distance_pairs"]:
            warn = (f" ⚠ 좌표가 완전히 같은 지점 쌍이 "
                    f"{got['zero_distance_pairs']}건 있습니다 — 데이터 확인이 "
                    "필요합니다.")
        if dry:
            return _list_page(request, db, user, notice=(
                f"[미리보기] 반경 {radius}m 기준 지점 {got['cameras']}개에서 "
                f"인접 관계 {got['links']}건이 만들어집니다. 저장하지 "
                f"않았습니다.{warn}"))

        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="CCTV 인접 관계 재생성",
                     after={"radius_m": radius, "links": got["links"],
                            "removed": got["removed"]})
        db.commit()
        return _list_page(request, db, user, notice=(
            f"반경 {radius}m 기준으로 인접 관계 {got['links']}건을 "
            f"만들었습니다(이전 자동 생성분 {got['removed']}건 교체). "
            f"사람이 지정한 상·하류 관계는 그대로입니다.{warn}"))

    @app.post("/settings/cameras/links/flow", response_class=HTMLResponse)
    def cameras_links_flow(request: Request, upper_id: str = Form(""),
                           lower_id: str = Form(""), remove: str = Form(""),
                           db: Session = Depends(get_db),
                           user: User = Depends(require_page(R.SETTINGS_OPS,
                                                             R.Action.EDIT))):
        """상류 → 하류 지정·해제.

        ⚠️ **좌표로는 알 수 없어 사람이 넣는다.** 지형과 배수 계통을 봐야 안다.
        그래서 자동 생성이 이 행을 건드리지 않는다(`auto=False`).
        """
        upper, lower = upper_id.strip(), lower_id.strip()
        if not upper or not lower:
            return _list_page(request, db, user, status_code=400,
                              errors=["상류와 하류 지점을 모두 고르세요."])
        # ⚠️ 두 카메라 모두에 담당 도메인이 하나라도 걸쳐 있어야 한다 —
        # 안 그러면 인파 담당자가 침수·교통 지점끼리의 상·하류를 마음대로
        # 정할 수 있었다(2026-08-22 전수점검).
        cam_u, cam_l = C.get(db, upper), C.get(db, lower)
        if cam_u is not None:
            _require_any_domain(user, _cam_domains(cam_u))
        if cam_l is not None:
            _require_any_domain(user, _cam_domains(cam_l))
        if remove:
            n = REL.clear_flow(db, upper, lower)
            audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                         ip=client_ip(request),
                         target=f"상·하류 해제 {upper}→{lower}")
            db.commit()
            return _list_page(request, db, user, notice=(
                f"상·하류 지정을 해제했습니다({n}건)."
                if n else "지정된 관계가 없었습니다."))

        errs = REL.set_flow(db, upper, lower)
        if errs:
            return _list_page(request, db, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"상·하류 지정 {upper}→{lower}")
        db.commit()
        return _list_page(request, db, user, notice=(
            f"{upper} 를 상류, {lower} 를 하류로 지정했습니다. "
            "상류에서 경계 이상 사건이 열리면 하류에 선행 경고가 뜹니다."))

    @app.post("/settings/cameras/{camera_id}/delete", response_class=HTMLResponse)
    def cameras_delete(camera_id: str, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_OPS,
                                                         R.Action.EDIT))):
        cam = C.get(db, camera_id)
        name = cam.name if cam else camera_id
        if cam is not None:
            # ⚠️ 삭제는 되돌릴 수 없다 — 이 카메라가 걸친 도메인 **전부**가
            # 담당이어야 지울 수 있다(다른 부서가 같이 쓰는 카메라를 혼자
            # 지우지 못하게, 2026-08-22 전수점검).
            _require_all_domains(user, _cam_domains(cam))
        ok, errs = C.delete(db, camera_id)
        if not ok:
            return _list_page(request, db, user, errors=errs, status_code=400)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target=f"CCTV 삭제 {camera_id}",
                     before={"name": name})
        db.commit()
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
        RESTREAM.remove_path(camera_id, db)
        return _list_page(request, db, user, notice=(
            f"{name} 을 삭제했습니다. 관련 ROI 설정도 함께 지워졌습니다."))

    @app.post("/settings/cameras/import/seoul", response_class=HTMLResponse)
    def cameras_import_seoul(request: Request, max_count: int = Form(20),
                             name_filter: str = Form(""),
                             db: Session = Depends(get_db),
                             user: User = Depends(require_page(R.SETTINGS_OPS,
                                                              R.Action.EDIT))):
        """서울 TOPIS 수집. **인증키가 필요 없다** — 공개 HLS 주소를 받는다."""
        try:
            rows = BULK.from_api_rows(
                TOPIS.fetch(max_count=max(1, min(int(max_count or 20), 60)),
                            name_filter=name_filter.strip()))
        except TOPIS.SourceError as e:
            return _list_page(request, db, user, errors=[str(e)], status_code=400)
        if not rows:
            note = "서울 교통정보시스템에서 지점을 찾지 못했습니다."
            if name_filter.strip():
                note += f" 이름에 「{name_filter.strip()}」 이 든 지점이 없습니다."
            return _list_page(request, db, user, errors=[note], status_code=400)
        checked = BULK.check(db, rows)
        return _list_page(request, db, user, preview=checked, preview_kind="api",
                          preview_payload=json.dumps(rows, ensure_ascii=False),
                          notice=(f"서울 {len(rows)}개소를 불러왔습니다. "
                                  "화질은 API가 알려주지 않으므로 등록 후 "
                                  "실측(probe_cctv_resolution.py)이 필요합니다."))

    # ---------- S-81 도메인별 ROI ----------
    @app.get("/settings/cameras/{camera_id}/roi", response_class=HTMLResponse)
    def roi_page(camera_id: str, request: Request, domain: str = "",
                 db: Session = Depends(get_db),
                 user: User = Depends(require_page(R.SETTINGS_OPS,
                                                   R.Action.VIEW))):
        cam = C.get(db, camera_id)
        if cam is None:
            return RedirectResponse(url="/settings/cameras", status_code=303)
        dm = C.domain_map(cam)
        enabled = [d.value for d in DOMAIN_ORDER if dm[d.value]["enabled"]]
        # 탐지 지정이 꺼진 도메인이라도 **저장된 ROI 가 있으면 탭을 남긴다.**
        #
        # 지정을 바꿨다고 애써 그린 영역이 화면에서 사라지면 「지워졌다」로
        # 읽힌다. 실제로는 camera_rois 에 그대로 남아 있고 다시 켜면 쓰인다.
        # 보이지 않으면 운영자는 다시 그리게 되고, 그것이 진짜 손실이다.
        kept = [d.value for d in DOMAIN_ORDER
                if d.value not in enabled and C.has_roi(cam, d.value)]
        usable = enabled + kept
        # ⚠️ MGR 은 이 카메라의 탭 중 자기 담당 도메인만 본다·고친다 —
        # 카메라 자체가 여러 부서에 걸쳐 있어도 남의 도메인 ROI 를 보거나
        # 건드리지 못하게 한다(2026-08-22 전수점검).
        scope = _scope(user)
        if scope is not None:
            usable = [d for d in usable if d in scope]
            if not usable:
                raise HTTPException(status_code=403,
                                    detail="이 카메라에 담당 도메인의 ROI 가 없습니다.")
        if not usable:
            usable = [R.Domain.FLOOD.value]
        # 기본 탭은 **쓰고 있는 도메인** 우선이다(enabled 가 앞에 있다).
        sel = domain if domain in usable else usable[0]
        # ⚠️ 2026-08-31(Phase 4) — 예전에는 파이프라인 store를 먼저
        # 봤다("돌고 있으면 그 프레임이 가장 싸다"). 침수·교통이 별도
        # 프로세스가 되며 이 프로세스엔 그 store가 없어, 소스에서 직접
        # 뽑는 이 폴백 하나로 단순화했다 — ROI는 설정 작업이라 애초에
        # 탐지 가동 여부와 무관해야 한다.
        snap, mask_status, snap_err = SNAP.grab(cam)

        return templates.TemplateResponse(request, "camera_roi.html", {
            **base_ctx(request, user), "active": "set-cameras",
            "cam": cam, "sel_domain": sel,
            # 세 번째 값은 「지금 탐지에 쓰이는가」다. 꺼져 있어도 ROI 는
            # 보관 중임을 화면이 말해 준다.
            "tabs": [(d, R.DOMAIN_LABELS[R.Domain(d)], d in enabled)
                     for d in usable],
            "sel_enabled": sel in enabled,
            "shapes": C.ROI_SHAPES.get(sel, []),
            "roi": C.roi_of(db, camera_id, sel),
            "snapshot": snap,
            "snap_error": snap_err,
            "mask_failed": mask_status in ("unavailable", "failed"),
            "can_edit": R.can(user.role, R.SETTINGS_OPS, R.Action.EDIT),
            # ★ 2026-08-28 — 지면 평면 캘리브레이션(호모그래피)을 이 화면
            # 에서 바로 찍을 수 있게 한다. 화면 좌표는 4점을 클릭해서
            # 채우고, 실제 좌표는 "가로×세로 몇 미터인 직사각형을 그
            # 순서로 쟀다"만 입력하면 되도록 단순화했다(calibration.py
            # GroundPlane 문서의 "직사각형 하나만 재면 됩니다" 그대로).
            # 세 도메인(침수·교통위험·인파)만 지면 평면을 쓴다
            # (routes_cameras.py::_calibration_from_form 참고).
            "calibration_ground": (
                calibration.of(cam, sel).ground
                if sel in ("flood", "traffic", "crowd") else None),
        })

    # ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 통행 방향 자동 생성
    # ("자동 생성" 버튼)은 여기 있었으나 traffic-service의
    # ``/api/traffic/roi-flow/propose``로 옮겼다. 이 핸들러는 실행 중인
    # 교통 추적기의 인메모리 상태(``TrafficPipelineRunner.
    # traffic_flow_samples()``)가 있어야 답할 수 있는데, 침수·교통이
    # 별도 프로세스가 된 뒤로는 그 상태가 이 프로세스(platform-shell)에
    # 없다. `camera_roi.html`의 호출부(app.js)도 함께 옮겼다.

    @app.post("/settings/cameras/{camera_id}/roi/{domain}")
    async def roi_save(camera_id: str, domain: str, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_OPS,
                                                         R.Action.EDIT))):
        if C.get(db, camera_id) is None:
            return {"ok": False, "errors": ["카메라를 찾을 수 없습니다."]}
        if domain not in C.ROI_SHAPES:
            return {"ok": False, "errors": ["알 수 없는 탐지서비스입니다."]}
        _require_domain(user, domain)
        payload = await request.json()
        data, errs = C.save_roi(db, camera_id, domain, payload, user)
        if errs:
            db.rollback()
            return {"ok": False, "errors": errs}
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request),
                     target=f"ROI 설정 {camera_id} / {domain}",
                     after={k: (len(v) if isinstance(v, list) else 0)
                            for k, v in data["shapes"].items()})
        db.commit()
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다
        # ⚠️ 이 메시지는 저장한 domain 과 무관하게 항상 "노면·인파" 얘기만
        #   했었다 — 침수·교통 ROI 를 저장해도 엉뚱한 문구가 나갔다(2026-08-22
        #   전수점검에서 발견). 도메인별로 실제 반영 여부에 맞는 문구를 낸다.
        #   ⚠️ 침수·교통은 "즉시 반영"이라고 아직 확정해 말할 수 없다 — 웹에서
        #   저장한 ROI 가 실제 상시 탐지 판정에 반영되는지는 별도 확인 중이다
        #   (침수는 판정 엔진이 legacy 파일을 읽는 문제, 교통은 ROI 를 아예
        #   안 읽는 문제가 각각 확인됐다). 고쳐지면 이 문구도 "곧바로
        #   반영됩니다"로 갱신한다.
        note = {
            "road": "노면 상시 탐지에는 곧바로 반영됩니다.",
            "crowd": "인파 상시 탐지는 서비스 재시작 후 반영됩니다.",
            # 2026-08-22 — 침수 판정 엔진이 DB의 ROI 를 매 틱 확인하도록
            # 고쳤다(common/roi.load_roi_for_camera + runner._block_loop).
            "flood": "침수 상시 탐지에는 곧바로 반영됩니다.",
            # 2026-08-22 — 교통 정체 판정도 congestion_roi 를 실제로 쓴다
            # (traffic_tracker.TrafficBehaviorTracker). 침수와 같은 지점에서
            # 함께 도는 구조라 반영 시점도 침수와 같다.
            "traffic": "교통위험 상시 탐지에는 곧바로 반영됩니다.",
        }.get(domain, "")

        # ⚠️ 저장 검증(validate_roi)은 「꼭짓점 3개 이상」만 본다. 그 위에서
        #   한 번 더 훑어 **막지 않고 알린다** — 2026-08-22부터 ROI 가 실제
        #   판정에 쓰이므로 잘못 그린 ROI 는 곧바로 오판이 되는데, 「안 보인다」와
        #   「없다」가 같아 보여 사람이 눈치채기 가장 어렵다. 정당한 설정인데
        #   걸리는 경우가 있어(일부러 좁게 그린 구역 등) **경고까지만** 한다.
        warnings: list[str] = []
        try:
            from ..core import roi_audit as RA
            # ⚠️ 변수 이름을 ``audit`` 으로 두면 이 모듈이 임포트한
            #   ``core.audit``(감사로그)를 가려, 바로 아래 audit.record()
            #   가 UnboundLocalError 로 터진다 — 실제로 그렇게 깨졌다.
            roi_check = RA.audit_shapes(camera_id, domain, data)
            warnings = [f"{f.shape}: {f.message}" for f in roi_check.findings
                        if f.severity in (RA.ERROR, RA.WARN)]
        except Exception:  # noqa: BLE001
            log.exception("ROI 진단 실패 camera=%s domain=%s", camera_id, domain)

        return {"ok": True, "message": f"저장했습니다. {note}".rstrip(),
                "warnings": warnings}

    # ---------- 일괄 등록 · 내보내기 ----------
    def _edit_guard():
        return require_page(R.SETTINGS_OPS, R.Action.EDIT)

    @app.get("/settings/cameras/export.xlsx")
    def cameras_export(db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_OPS,
                                                         R.Action.VIEW))):
        """등록된 CCTV·영상 전체를 엑셀로 내려받는다.

        업로드 양식과 열이 같아, 받아서 고친 뒤 그대로 다시 올릴 수 있다.
        ⚠️ MGR 은 담당 도메인과 겹치는 카메라만 받는다(2026-08-22 전수점검
        — 예전에는 전체를 그대로 내려받을 수 있었다).
        """
        scope = _scope(user)
        ids = None
        if scope is not None:
            ids = {c.id for c in C.list_all(db) if _cam_domains(c) & scope}
        try:
            data = BULK.export_excel(db, camera_ids=ids)
        except RuntimeError as e:
            return Response(str(e), status_code=503, media_type="text/plain; charset=utf-8")
        name = "UrbanGuard_CCTV목록.xlsx"
        quoted = urllib.parse.quote(name)
        return Response(data, media_type=XLSX_TYPE, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})

    @app.get("/settings/cameras/template.xlsx")
    def cameras_template(user: User = Depends(require_page(R.SETTINGS_OPS,
                                                           R.Action.VIEW))):
        """업로드 양식. 열 설명과 예시 한 줄이 들어 있다."""
        try:
            data = BULK.template_excel()
        except RuntimeError as e:
            return Response(str(e), status_code=503, media_type="text/plain; charset=utf-8")
        quoted = urllib.parse.quote("UrbanGuard_CCTV등록양식.xlsx")
        return Response(data, media_type=XLSX_TYPE, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})

    @app.post("/settings/cameras/import/fetch", response_class=HTMLResponse)
    def cameras_import_fetch(request: Request, region: str = Form("busan"),
                             min_height: int = Form(0),
                             db: Session = Depends(get_db),
                             user: User = Depends(_edit_guard())):
        """교통정보 API에서 지점을 수집해 **미리보기만** 만든다."""
        try:
            rows = BULK.from_api_rows(
                SRC.fetch(region, min_height=min_height or None))
        except SRC.SourceError as e:
            return _list_page(request, db, user, errors=[str(e)], status_code=400)
        if not rows:
            label = SRC.REGIONS.get(region, (region, None))[0]
            note = (f"{label}에서 실시간 스트리밍 CCTV를 찾지 못했습니다.")
            if min_height:
                note += (f" 최소 해상도를 {min_height}p로 두었습니다 — "
                         "「제한 없음」으로 다시 시도해 보세요.")
            return _list_page(request, db, user, errors=[note], status_code=400)
        checked = BULK.check(db, rows)
        return _list_page(request, db, user, preview=checked, preview_kind="api",
                          preview_payload=json.dumps(rows, ensure_ascii=False),
                          notice=f"{len(rows)}개소를 불러왔습니다. "
                                 "아래에서 확인한 뒤 등록하세요.")

    @app.post("/settings/cameras/import/excel", response_class=HTMLResponse)
    async def cameras_import_excel(request: Request,
                                   file: UploadFile = File(...),
                                   db: Session = Depends(get_db),
                                   user: User = Depends(_edit_guard())):
        """엑셀을 읽어 **미리보기만** 만든다."""
        if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
            return _list_page(request, db, user,
                              errors=["엑셀 파일(.xlsx)만 올릴 수 있습니다. "
                                      "예전 .xls 는 지원하지 않습니다."],
                              status_code=400)
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            return _list_page(request, db, user,
                              errors=["파일이 너무 큽니다. 5MB 이하로 올려 주세요."],
                              status_code=400)
        try:
            rows, errs = BULK.parse_excel(data)
        except RuntimeError as e:
            return _list_page(request, db, user, errors=[str(e)], status_code=503)
        if errs:
            return _list_page(request, db, user, errors=errs, status_code=400)
        if not rows:
            return _list_page(request, db, user,
                              errors=["읽을 행이 없습니다."], status_code=400)
        checked = BULK.check(db, rows)
        return _list_page(request, db, user, preview=checked, preview_kind="excel",
                          preview_payload=json.dumps(rows, ensure_ascii=False),
                          notice=f"{len(rows)}행을 읽었습니다. "
                                 "아래에서 확인한 뒤 등록하세요.")

    @app.post("/settings/cameras/import/apply", response_class=HTMLResponse)
    async def cameras_import_apply(request: Request,
                                   db: Session = Depends(get_db),
                                   user: User = Depends(_edit_guard())):
        """미리보기에서 **고른 행만** 실제로 등록한다."""
        form = await request.form()
        try:
            rows = json.loads(form.get("payload") or "[]")
        except json.JSONDecodeError:
            return _list_page(request, db, user,
                              errors=["미리보기 데이터를 읽지 못했습니다. "
                                      "다시 불러오세요."], status_code=400)
        picked = {str(v) for v in form.getlist("pick")}
        if picked:
            rows = [r for r in rows if str(r.get("id")) in picked]

        # ⚠️ 일괄등록은 행마다 도메인이 제각각이라 한 화면 권한으로 판단할
        # 수 없다 — MGR 은 자기 담당이 아닌 도메인을 「사용」으로 켜는 행이
        # 하나라도 있으면 전체를 거부한다(2026-08-22 전수점검, 예전에는
        # 아무 제약이 없었다).
        scope = _scope(user)
        if scope is not None:
            bad_rows = []
            for r in rows:
                wanted = {d for d in R.DOMAIN_SHORT
                         if BULK._flag(r.get(f"use_{d}"))}
                if not (wanted <= scope):
                    bad_rows.append(str(r.get("id") or r.get("name") or "?"))
            if bad_rows:
                return _list_page(request, db, user, status_code=403, errors=[
                    "담당하지 않는 도메인을 사용으로 지정한 행이 있어 "
                    f"전체를 등록하지 않았습니다: {', '.join(bad_rows[:10])}"
                    + (" 외" if len(bad_rows) > 10 else "")])
        if not rows:
            return _list_page(request, db, user,
                              errors=["등록할 항목을 하나도 고르지 않았습니다."],
                              status_code=400)

        update_existing = form.get("update_existing") is not None
        res = BULK.apply(db, rows, update_existing=update_existing)
        audit.record(db, action=audit.SETTINGS_UPDATE, user=user,
                     ip=client_ip(request), target="CCTV 일괄 등록",
                     after={k: v for k, v in res.items() if k != "errors"})
        db.commit()
        config_rev.bump()
        config_rev_bridge.notify()  # ⚠️ 2026-08-31 — road-service(별도 프로세스)도 즉시 깨운다

        msg = (f"신규 {res['created']}건 · 갱신 {res['updated']}건 · "
               f"건너뜀 {res['skipped']}건 · 실패 {res['failed']}건.")
        if res["created"] or res["updated"]:
            # ⚠️ 예전에는 "상시 탐지 지정은 재시작 후 반영"이라고 도메인
            #   구분 없이 뭉뚱그렸다 — 노면은 실제로 곧바로 반영되는데도 그렇게
            #   말해 단일등록 화면(301-303행)과 다른 얘기를 하고 있었다
            #   (2026-08-22 전수점검에서 발견). 아는 만큼만 정확히 말한다.
            msg += (" 침수·교통위험·노면 상시 탐지는 곧바로 반영됩니다. "
                    "⚠ 인파 상시 탐지는 서비스 재시작 후 반영됩니다.")
        return _list_page(request, db, user, notice=msg,
                          errors=res["errors"][:20])
