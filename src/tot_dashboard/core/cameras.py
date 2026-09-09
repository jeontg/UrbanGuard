"""CCTV 카메라 등록·관리 (S-80) 와 도메인별 ROI (S-81).

기존에는 침수 도메인 전용 `blocks.json` 에 인파 설정이 얹혀 있고 노면은 자리가
없었다. 세 도메인이 **같은 카메라를 공유**하되 도메인별 설정은 따로 갖도록
구조를 바꾼다.

분석 방식은 카메라마다 관리자가 정한다.
  - **상시(continuous)** — 파이프라인이 계속 돌린다
  - **선택(on-demand)** — 화면에서 카메라를 고를 때만 분석한다
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cctv_sources as SRC
from . import regions as RG
from . import restream
from . import settings as ug_settings
from .models import Camera, CameraDomain, CameraRoi
from .roles import Domain

log = logging.getLogger("urbanguard.cameras")

ID_RE = re.compile(r"^[A-Z][A-Z0-9\-_]{2,63}$")
SOURCE_TYPES = {
    "hls": "HLS 스트림 (실시간 CCTV)",
    "video": "동영상 파일",
    "synthetic": "합성 데이터 (시험용)",
}

# 도메인별 ROI 도형 정의 — 같은 카메라라도 보는 영역이 다르다.
ROI_SHAPES = {
    Domain.FLOOD.value: [
        ("road_roi", "도로 ROI", "polygon", True),
        ("low_point_roi", "저지대 ROI", "polygon", False),
        # ⚠️ 2026-08-23 — "차선 기준선"에서 개명. 차선(주행 차로)을 구분하는
        # 선이 아니라(카메라당 1개뿐 — 차로별로 여러 개 두는 구조가 아님),
        # 물 면적 비율과 무관하게 "이 지점에 물이 닿으면 그 자체로 위험"
        # 이라고 보는 한 지점(트립와이어)이라 실제 용도에 맞게 바꿨다
        # (alert_engine.py 의 water_crosses_lane 규칙 참고 — 비율 조건 없이
        # 단독으로 4단계「위험」을 발동시킨다).
        ("lane_threshold_line", "침수 경계선", "line", False),
    ],
    # 2026-08-21 flood/traffic 도메인 분리로 신설.
    Domain.TRAFFIC.value: [
        # 차량 흐름을 재는 구역. 화면 전체를 재면 보도·건물까지 들어가
        # 정체 판정이 흐려진다.
        ("congestion_roi", "정체 감시 구역", "polygon", True),
        # ⚠️ 2026-08-26 — 이 기준선은 저장만 되고 **쓰는 곳이 없다**(전
        # 저장소에 소비자 0곳, 실측 확인). 단일 선분이라 원근 왜곡을 보정할
        # 수 없어(calibration.py:128-131이 경고하는 바로 그 함정) 애초에
        # 정확한 속도 환산에 못 쓴다 — 속도(km/h)는 지면 캘리브레이션
        # (`routes_cameras.py`의 4점 GroundPlane, 인파 도메인에서 이미
        # 검증된 방식)으로 대신 구현했다(`docs/202608260842/` 계획 Phase 2).
        # 이 도형 자체는 지우지 않았다 — 이미 그린 지점이 있을 수 있어
        # 확인 없이 삭제하지 않는다(교체는 별도 검토 대상).
        ("speed_line", "속도측정 기준선(미사용)", "line", False),
        # 2026-08-26 — 보행자 도로 진입 판정에서, 신호 대기 중인 횡단보도
        # 보행자를 오탐에서 뺀다(교통 돌발상황 확장 Phase 1).
        ("pedestrian_exempt_roi", "횡단보도 제외 구역", "polygon", False),
        # 2026-08-26 — 역주행 판정의 "정상 방향" 기준(Phase 4). kind가
        # "arrows"인 유일한 항목 — 저장 형태는 폴리곤과 같은 중첩 리스트지만
        # 항목마다 점이 **정확히 2개**(시작→끝이 곧 방향)라는 제약이 다르다.
        # required=False인 이유 — 화살표를 안 그린 카메라는 역주행 판정을
        # 그냥 안 하면 된다(정체·속도 판정은 이 도형과 무관하게 그대로 돈다).
        ("flow_arrows", "통행 방향(화살표)", "arrows", False),
    ],
    Domain.CROWD.value: [
        # 교통 CCTV는 도로를 향해 있어 화면 전체를 재면 차도까지 분모에 들어간다.
        # 보행 구역만 지정하면 인원·밀집도가 실제와 가까워진다.
        ("analysis_roi", "분석 영역 (보행 구역)", "polygon", False),
        ("intrusion_roi", "침입 금지 구역", "polygon", False),
        ("loiter_roi", "배회 감시 구역", "polygon", False),
    ],
    Domain.ROAD.value: [
        ("analysis_roi", "노면 분석 구간", "polygon", False),
    ],
}
# 이 도형이 없으면 해당 도메인의 판정이 부정확해진다.
REQUIRED_SHAPE = {Domain.FLOOD.value: "road_roi",
                  Domain.TRAFFIC.value: "congestion_roi"}

# ``to_block_dict`` 이 블록 dict 안에 **하위 키로** 실어 보내는 도메인들.
# 침수(FLOOD)는 블록 자체가 침수 파이프라인용이라 최상위에 펼쳐지므로 빠진다.
_SUB_DOMAINS = (Domain.TRAFFIC.value, Domain.CROWD.value, Domain.ROAD.value)
_SUB_DOMAIN_KEYS = frozenset(_SUB_DOMAINS)


# --- 조회 -------------------------------------------------------------------
def list_all(db: Session, *, active_only: bool = False) -> list[Camera]:
    stmt = select(Camera).order_by(Camera.id)
    if active_only:
        stmt = stmt.where(Camera.is_active.is_(True))
    return list(db.scalars(stmt).all())


def get(db: Session, camera_id: str) -> Camera | None:
    return db.get(Camera, camera_id)


def for_domain(db: Session, domain: str, *, continuous: bool | None = None,
               active_only: bool = True) -> list[Camera]:
    """특정 도메인이 쓰는 카메라 목록.

    ``continuous`` 를 주면 상시/선택 분석 대상만 걸러낸다. 파이프라인은
    ``continuous=True`` 로, 화면의 선택 목록은 ``continuous=None`` 으로 부른다.
    """
    stmt = (select(Camera).join(CameraDomain)
            .where(CameraDomain.domain == domain,
                   CameraDomain.enabled.is_(True))
            .order_by(Camera.id))
    if continuous is not None:
        stmt = stmt.where(CameraDomain.continuous.is_(continuous))
    if active_only:
        stmt = stmt.where(Camera.is_active.is_(True))
    return list(db.scalars(stmt).all())


def domain_map(cam: Camera) -> dict[str, dict]:
    """카메라의 도메인 설정을 화면에서 쓰기 좋은 형태로."""
    out = {}
    for d in Domain:
        row = cam.domain_row(d.value)
        out[d.value] = {
            "enabled": bool(row and row.enabled),
            "continuous": bool(row and row.continuous),
            "config": (row.config if row else None) or {},
        }
    return out


# --- 검증 -------------------------------------------------------------------
def validate(data: dict, *, existing_id: str | None = None,
             db: Session | None = None) -> list[str]:
    """저장 전 검증.

    카메라 하나가 잘못 들어가면 파이프라인 스레드가 기동 중 죽어 **다른 지점의
    탐지까지 멈춘다.** 막는 편이 훨씬 싸다.
    """
    errs: list[str] = []
    cid = (data.get("id") or "").strip()
    if not ID_RE.match(cid):
        errs.append("ID는 대문자로 시작하고 영문 대문자·숫자·하이픈으로 "
                    "3~64자여야 합니다. 예: CAM-SEOMYEON")
    elif existing_id is None and db is not None and get(db, cid) is not None:
        errs.append(f"이미 존재하는 ID입니다: {cid}")

    if not (data.get("name") or "").strip():
        errs.append("카메라 이름을 입력하세요.")

    for key, label, lo, hi in (("lat", "위도", 33.0, 39.0),
                               ("lng", "경도", 124.0, 132.0)):
        raw = data.get(key)
        if raw in (None, ""):
            errs.append(f"{label}를 입력하세요.")
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            errs.append(f"{label}는 숫자여야 합니다.")
            continue
        # 국내 범위를 벗어나면 지도에서 나머지 지점이 한 점으로 뭉친다.
        if not (lo <= v <= hi):
            errs.append(f"{label} 값이 국내 범위({lo}~{hi})를 벗어납니다: {v}")

    errs += RG.validate(data.get("sido"), data.get("sigungu"))
    errs += _validate_aim(data)

    stype = data.get("source_type")
    if stype not in SOURCE_TYPES:
        errs.append("영상 소스 종류를 선택하세요.")
    elif stype == "hls":
        if not (data.get("source_url") or "").strip().startswith(("http://", "https://")):
            errs.append("HLS 스트림 주소는 http:// 또는 https:// 로 시작해야 합니다.")
    elif stype == "video":
        if not (data.get("source_path") or "").strip():
            errs.append("동영상 파일 경로를 입력하세요.")
    return errs


# --- 방향 정보 (S-63 사각지대 분석의 선행 조건) -------------------------------
#
# 근거는 KLID 제안요청서 SFR-14 「CCTV 의 좌표정보 및 방향각(상하/좌우)을 활용해
# GIS 시각화」「각 CCTV 를 설치 목적별로 분류」다.
#
# ⚠️ **비워 두는 것이 기본이다.** 방향을 모르는데 0 으로 채우면 「전부 북쪽을
# 본다」가 되어 커버리지 분석이 통째로 거짓이 된다. 빈 값은 빈 값으로 남긴다.

# 설치 목적. 빈 값은 「미지정」이다.
PURPOSES = {"", "crime", "disaster", "traffic", "facility", "other"}
PURPOSE_LABELS = {
    "crime": "방범", "disaster": "재난", "traffic": "교통",
    "facility": "시설", "other": "기타",
}

# (키, 이름, 최소, 최대)
_AIM_FIELDS = (
    ("bearing_deg", "방위각", 0, 359),
    ("tilt_deg", "상하각", -90, 90),
    ("fov_deg", "화각", 1, 360),
)


def _validate_aim(data: dict) -> list[str]:
    errs: list[str] = []
    for key, label, lo, hi in _AIM_FIELDS:
        raw = data.get(key)
        if raw in (None, ""):
            continue        # 비워 두는 것이 정상이다
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            errs.append(f"{label}은 정수여야 합니다.")
            continue
        if not (lo <= v <= hi):
            errs.append(f"{label}은 {lo}~{hi} 범위여야 합니다: {v}")
    if (data.get("purpose") or "").strip() not in PURPOSES:
        errs.append("설치 목적이 올바르지 않습니다.")
    return errs


def _apply_aim(cam: Camera, data: dict) -> None:
    for key, _label, _lo, _hi in _AIM_FIELDS:
        raw = data.get(key)
        # ⚠️ 키가 폼에 아예 없으면 **건드리지 않는다.** 일괄 등록처럼 방향
        # 칸이 없는 경로에서 기존 값을 지워 버리면 안 된다.
        if key not in data:
            continue
        setattr(cam, key, None if raw in (None, "") else int(str(raw).strip()))
    if "purpose" in data:
        cam.purpose = (data.get("purpose") or "").strip()


# --- 저장 -------------------------------------------------------------------
def _apply(cam: Camera, data: dict) -> None:
    cam.name = (data.get("name") or "").strip()
    cam.dept = (data.get("dept") or "").strip()
    cam.lat = float(data["lat"])
    cam.lng = float(data["lng"])
    # 행정구역. 시/도를 비워 두고 저장하면 좌표로 **시/도만** 채운다.
    # 구·군은 채우지 않는다 — 사각형으로는 구를 가를 수 없다.
    sido = (data.get("sido") or "").strip()
    cam.sido = sido or RG.fallback_sido(cam.lat, cam.lng)
    cam.sigungu = (data.get("sigungu") or "").strip() if cam.sido else ""
    cam.source_type = data["source_type"]
    cam.cctv_name = (data.get("cctv_name") or "").strip()
    cam.note = (data.get("note") or "").strip()
    if cam.source_type == "hls":
        cam.source_url = (data.get("source_url") or "").strip()
        cam.source_path = ""
    elif cam.source_type == "video":
        cam.source_path = (data.get("source_path") or "").strip()
        cam.source_url = ""
    else:
        cam.source_url = cam.source_path = ""
    _apply_aim(cam, data)


def create(db: Session, data: dict) -> tuple[Camera | None, list[str]]:
    errs = validate(data, db=db)
    if errs:
        return None, errs
    cam = Camera(id=data["id"].strip())
    _apply(cam, data)
    db.add(cam)
    db.flush()
    log.info("카메라 추가 id=%s", cam.id)
    return cam, []


def update(db: Session, camera_id: str, data: dict) -> tuple[Camera | None, list[str]]:
    cam = get(db, camera_id)
    if cam is None:
        return None, [f"카메라를 찾을 수 없습니다: {camera_id}"]
    data = dict(data)
    # ID 변경은 허용하지 않는다 — 이벤트 이력·ROI가 ID로 묶여 있다.
    data["id"] = camera_id
    errs = validate(data, existing_id=camera_id, db=db)
    if errs:
        return None, errs
    _apply(cam, data)
    log.info("카메라 수정 id=%s", camera_id)
    return cam, []


def set_domains(db: Session, cam: Camera, selections: dict[str, dict]) -> None:
    """도메인 사용 여부와 분석 방식을 한꺼번에 반영한다.

    selections = {"flood": {"enabled": True, "continuous": True}, ...}
    """
    for dom, sel in selections.items():
        row = cam.domain_row(dom)
        if row is None:
            row = CameraDomain(camera_id=cam.id, domain=dom)
            db.add(row)
            cam.domains.append(row)
        row.enabled = bool(sel.get("enabled"))
        # 쓰지 않는 도메인이 상시 분석으로 남아 있으면 파이프라인이 헛돈다.
        row.continuous = bool(sel.get("continuous")) and row.enabled


def region_of(cam: Camera) -> str | None:
    """카메라가 속한 시/도 키. **저장된 값을 쓰고**, 없으면 좌표로 유추한다.

    저장을 우선하는 이유 — 사각형은 행정경계가 아니라서, 경계 가까운 지점은
    이웃 시도로 잘못 잡힌다. 사람이 지정한 값이 있으면 그것이 맞다.
    """
    return (cam.sido or "").strip() or SRC.region_of(cam.lat, cam.lng)


def region_label_of(cam: Camera) -> str:
    """「서울특별시 강남구」 처럼 구·군까지. 모르면 「지역 미상」."""
    return RG.label_of(region_of(cam), cam.sigungu)


def group_by_region(cameras: list[Camera]) -> list[dict]:
    """시/도별로 묶어 화면에 쓰기 좋은 형태로. 등록 수가 많은 지역부터.

    지역을 알 수 없는 카메라도 **버리지 않고 「지역 미상」으로 묶는다** —
    안 보이면 해지도 못 한다. 시/도 안에서 구·군 내역도 함께 센다.
    """
    buckets: dict[str | None, list[Camera]] = {}
    for cam in cameras:
        buckets.setdefault(region_of(cam), []).append(cam)
    out = []
    for key, rows in buckets.items():
        sub: dict[str, int] = {}
        for cam in rows:
            sub[(cam.sigungu or "").strip() or "구·군 미지정"] = \
                sub.get((cam.sigungu or "").strip() or "구·군 미지정", 0) + 1
        out.append({
            "key": key or "", "label": SRC.region_label(key),
            "count": len(rows), "cameras": rows,
            "sigungu": sorted(sub.items(), key=lambda x: (-x[1], x[0])),
        })
    out.sort(key=lambda x: (-x["count"], x["label"]))
    return out


def by_region(db: Session, region: str) -> list[Camera]:
    """해당 시도의 카메라만. ``region`` 이 빈 값이면 「지역 미상」을 준다."""
    return [c for c in list_all(db) if (region_of(c) or "") == (region or "")]


def delete_region(db: Session, region: str) -> tuple[int, list[str]]:
    """지역 단위 일괄 해지. 지운 수를 돌려준다.

    **마지막 남은 카메라는 남긴다** — 한 대도 없으면 화면과 파이프라인이
    빈 채로 돌아 「설정이 날아간 것」과 구분되지 않는다(단건 삭제와 같은 규칙).
    """
    targets = by_region(db, region)
    if not targets:
        return 0, [f"{SRC.region_label(region or None)}에 등록된 지점이 없습니다."]
    total = len(list_all(db))
    if len(targets) >= total:
        return 0, ["모든 지점을 한 번에 해지할 수는 없습니다. "
                   "최소 한 지점은 남겨 두세요."]
    for cam in targets:
        db.delete(cam)   # camera_domains·camera_rois 는 CASCADE
    log.info("카메라 지역 해지 region=%s %d대", region or "미상", len(targets))
    return len(targets), []


def delete(db: Session, camera_id: str) -> tuple[bool, list[str]]:
    cam = get(db, camera_id)
    if cam is None:
        return False, [f"카메라를 찾을 수 없습니다: {camera_id}"]
    remaining = db.scalar(select(Camera).where(Camera.id != camera_id))
    if remaining is None:
        return False, ["마지막 남은 카메라는 삭제할 수 없습니다."]
    db.delete(cam)   # camera_domains·camera_rois 는 CASCADE
    log.info("카메라 삭제 id=%s", camera_id)
    return True, []


# --- ROI --------------------------------------------------------------------
def roi_of(db: Session, camera_id: str, domain: str) -> dict:
    row = db.scalar(select(CameraRoi).where(CameraRoi.camera_id == camera_id,
                                            CameraRoi.domain == domain))
    if row is None:
        return {"frame_width": 0, "frame_height": 0, "shapes": {}}
    return {"frame_width": row.frame_width, "frame_height": row.frame_height,
            "shapes": row.shapes or {}}


def _clean(points, *, w: int, h: int, min_points: int) -> list:
    """좌표를 정수로 맞추고 프레임 밖 값을 잘라낸다.

    프레임을 벗어난 좌표는 마스크 연산에서 조용히 잘려, 화면에 보이는 영역과
    실제 판정 영역이 달라진다.
    """
    out = []
    for poly in points or []:
        pts = []
        for p in poly or []:
            try:
                x, y = int(round(float(p[0]))), int(round(float(p[1])))
            except (TypeError, ValueError, IndexError):
                continue
            if w > 0:
                x = max(0, min(w - 1, x))
            if h > 0:
                y = max(0, min(h - 1, y))
            pts.append([x, y])
        if len(pts) >= min_points:
            out.append(pts)
    return out


def _clean_arrows(raw, *, w: int, h: int) -> list:
    """통행 방향 화살표를 정리한다. 좌표 정리는 :func:`_clean` 과 같지만,
    항목마다 점이 **정확히 2개**(시작→끝이 방향)여야 하고 **길이가 0이면
    안 된다**(시작점=끝점이면 방향을 알 수 없다) — 폴리곤과 다른 제약이라
    별도 함수로 뺐다.
    """
    out = []
    for arr in raw or []:
        if not (isinstance(arr, list) and len(arr) == 2):
            continue
        pts = []
        for p in arr:
            try:
                x, y = int(round(float(p[0]))), int(round(float(p[1])))
            except (TypeError, ValueError, IndexError):
                pts = []
                break
            if w > 0:
                x = max(0, min(w - 1, x))
            if h > 0:
                y = max(0, min(h - 1, y))
            pts.append([x, y])
        if len(pts) == 2 and pts[0] != pts[1]:
            out.append(pts)
    return out


def validate_roi(domain: str, payload: dict) -> list[str]:
    errs: list[str] = []
    try:
        w = int(payload.get("frame_width") or 0)
        h = int(payload.get("frame_height") or 0)
    except (TypeError, ValueError):
        return ["프레임 크기가 올바르지 않습니다."]
    if w <= 0 or h <= 0:
        errs.append("정지영상 크기를 확인할 수 없습니다. 영상을 다시 불러오세요.")

    shapes = payload.get("shapes") or {}
    for key, label, kind, required in ROI_SHAPES.get(domain, []):
        raw = shapes.get(key) or []
        if kind == "line":
            pts = raw if raw and isinstance(raw[0], (int, float)) is False else []
            if pts and len(pts) != 2:
                errs.append(f"{label}은 점 2개로 이뤄져야 합니다.")
        elif kind == "arrows":
            # ⚠️ 2026-08-26 — 역주행 판정의 "정상 방향" 기준(Phase 4).
            # 폴리곤과 저장 구조(점 목록의 목록)는 같지만 항목마다 점이
            # 정확히 2개(시작→끝)여야 방향이 정의된다.
            malformed = any(
                not (isinstance(a, list) and len(a) == 2) for a in raw)
            cleaned = _clean_arrows(raw, w=w, h=h)
            if malformed or len(cleaned) < len([a for a in raw if isinstance(a, list)]):
                errs.append(f"{label}는 항목마다 점이 정확히 2개(시작→끝)이고 "
                           "길이가 0이 아니어야 합니다.")
            if required and not cleaned:
                errs.append(f"{label}는 화살표가 최소 하나 필요합니다.")
        else:
            cleaned = _clean(raw, w=w, h=h, min_points=3)
            if required and not cleaned:
                errs.append(f"{label}는 꼭짓점 3개 이상인 다각형이 최소 하나 필요합니다.")
    return errs


def save_roi(db: Session, camera_id: str, domain: str, payload: dict,
             user=None) -> tuple[dict | None, list[str]]:
    errs = validate_roi(domain, payload)
    if errs:
        return None, errs
    w, h = int(payload["frame_width"]), int(payload["frame_height"])
    shapes_in = payload.get("shapes") or {}
    shapes: dict = {}
    for key, _label, kind, _req in ROI_SHAPES.get(domain, []):
        raw = shapes_in.get(key) or []
        if kind == "line":
            cleaned = _clean([raw], w=w, h=h, min_points=2)
            shapes[key] = cleaned[0] if cleaned else []
        elif kind == "arrows":
            shapes[key] = _clean_arrows(raw, w=w, h=h)
        else:
            shapes[key] = _clean(raw, w=w, h=h, min_points=3)

    row = db.scalar(select(CameraRoi).where(CameraRoi.camera_id == camera_id,
                                           CameraRoi.domain == domain))
    if row is None:
        row = CameraRoi(camera_id=camera_id, domain=domain)
        db.add(row)
    row.frame_width, row.frame_height = w, h
    row.shapes = shapes
    row.updated_by = getattr(user, "id", None)
    log.info("ROI 저장 camera=%s domain=%s", camera_id, domain)
    return {"frame_width": w, "frame_height": h, "shapes": shapes}, []


def has_roi(cam: Camera, domain: str) -> bool:
    """이 카메라·도메인에 **저장된 ROI 가 있는가.**

    탐지 지정이 꺼져 있어도 ROI 는 지우지 않는다(``camera_rois`` 는 별도
    테이블이다). 화면이 이 사실을 보여 주지 않으면 운영자는 「지워졌다」고
    보고 다시 그리게 된다.
    """
    row = cam.roi_row(domain)
    if row is None:
        return False
    return any(v for v in (row.shapes or {}).values())


def roi_status(db: Session, cameras: list[Camera]) -> dict[str, dict]:
    """카메라별·도메인별 ROI 설정 현황."""
    out: dict[str, dict] = {}
    for cam in cameras:
        per: dict[str, dict] = {}
        for d in Domain:
            row = cam.roi_row(d.value)
            shapes = (row.shapes if row else None) or {}
            need = REQUIRED_SHAPE.get(d.value)
            per[d.value] = {
                "exists": row is not None,
                "counts": {k: (len(v) if isinstance(v, list) else 0)
                           for k, v in shapes.items()},
                "ok": bool(shapes.get(need)) if need else bool(shapes),
            }
        out[cam.id] = per
    return out


# --- 침수 파이프라인 호환 ----------------------------------------------------
def to_block_dict(cam: Camera) -> dict:
    """기존 파이프라인이 기대하는 blocks.json 항목 형태로 변환한다.

    파이프라인(`runner.py`)과 도메인 코드가 이 구조를 그대로 쓰고 있어, 한 번에
    전부 고치는 대신 경계에서 변환한다. 탐지 코드를 건드리지 않는 편이 안전하다.
    """
    row = cam.domain_row(Domain.FLOOD.value)
    trow = cam.domain_row(Domain.TRAFFIC.value)
    cfg = (row.config if row else None) or {}
    src: dict = {"type": cam.source_type}
    if cam.source_type == "hls":
        src["url"] = cam.source_url
        if cam.cctv_name:
            src["cctv_name"] = cam.cctv_name
        # ★ 2026-08-28 — CCTV 재배포 허브(MediaMTX). 침수+교통위험(runner.py)과
        # 노면(road/live_analyzer.py)은 여기서 만든 block["source"]["url"]을
        # 그대로 신뢰하므로, 치환을 이 한 곳에만 두면 두 소비 지점을 건드릴
        # 필요가 없다(인파는 Camera ORM에서 직접 읽어 별도 처리 — continuous.py
        # 참고). 재배포가 꺼져 있으면 원본 URL 그대로다.
        #
        # ⚠️ **db=None(캐시만 보기)으로 짰다가 실사용 점검에서 되돌렸다**
        # (2026-08-28). `main.py`의 `BLOCKS = _load_blocks()`는 **모듈
        # 최상단에서, lifespan 시작 전에** 이 함수를 호출한다 — 그 시점에는
        # `ug_settings._cache`가 아직 비어 있어(`load_all()`은 lifespan
        # 안에서만 실행됨) `restream_enabled()`가 무조건 `DEFAULTS`(꺼짐)로
        # 떨어진다. 그런데 재배포는 **다른(이전) 프로세스가 관리 화면이나
        # 스크립트로 DB에 이미 켜 둔 값**을 이 새 프로세스가 처음 읽어야
        # 하는 경우라 "같은 프로세스면 set_value가 캐시도 갱신한다"는 전제가
        # 성립하지 않는다 — 재배포를 켰는데 서비스를 재기동해도 여전히
        # 원본 URL로 붙는 결함으로 실측 확인됐다. `runner.py`의 rainfall
        # 설정 조회(406-426행)와 같은 패턴으로 되돌린다: 매번 짧은 세션을
        # 새로 연다. `to_block_dict()` 호출 빈도(기동·동적 재구성·상시
        # 재스캔 시점뿐)를 감안하면 이 비용은 무시할 만하다 — 시험 스위트가
        # 한때 느려졌던 진짜 원인은 이 세션 비용이 아니라 별도로 찾은
        # `_block_loop`의 예외 처리 결함 두 건이었다(테스트 통과 시간이
        # 그 결함들을 고친 뒤에야 실제로 좋아진 것으로 확인).
        try:
            from .db import get_session as _get_settings_session
            _db = _get_settings_session()
            try:
                # ★ 2026-08-28 — 재배포가 켜져 있어도, 원본 서버 특성상
                # MediaMTX와 근본적으로 안 맞는 카메라는 예외로 원본
                # 직결을 유지한다(restream.is_excluded 주석 참고).
                if (ug_settings.restream_enabled(_db)
                        and not restream.is_excluded(cam.id, _db)):
                    src["origin_url"] = cam.source_url  # 진단용으로 원본을 남긴다
                    src["url"] = restream.rtsp_url(cam.id, _db)
            finally:
                _db.close()
        except Exception as e:  # noqa: BLE001
            log.warning("재배포 설정 조회 실패, 원본 URL로 진행 camera=%s: %s",
                       cam.id, str(e)[:120])
    elif cam.source_type == "video":
        src["path"] = cam.source_path

    block = {
        "id": cam.id,
        "name": cam.name,
        "dept": cam.dept,
        # 행정구역도 함께 넘긴다. 상황판이 지역으로 거를 때 카메라를 다시
        # 조회하지 않아도 되게 하려는 것이다(지도는 매 요청 그린다).
        "sido": (cam.sido or "").strip(),
        "sigungu": (cam.sigungu or "").strip(),
        "region_label": region_label_of(cam),
        "coordinates": {"lat": cam.lat, "lng": cam.lng},
        "source": src,
        # ⚠️ 2026-08-23 — 「탐지 지정이 꺼져도 ROI는 보관」 원칙 때문에,
        # `row`(flood domain_row)는 `enabled=False`여도 존재할 수 있다.
        # 이 값이 없으면 `runner.py`가 "이 블록에 water_model이 있으니
        # 침수 판정을 돌려도 된다"고 착각해, 침수를 꺼 둔(교통만 켠)
        # 카메라에서 조용히 침수 알림이 나갈 수 있다 — 실제 판정 실행
        # 여부는 반드시 이 값으로만 판단해야 한다.
        "flood_enabled": bool(row and row.enabled),
        # ★ 2026-08-28 — flood_enabled 와 대칭. 이 값이 없어 `runner.py`가
        # 「이 블록이 (침수∪교통) 상시 목록에 있으니 교통위험 판정도 당연히
        # 돈다」고 가정하고 있었다. 그런데 이 목록은 침수·교통 어느 한쪽만
        # 상시라도 올라온다 — 침수만 상시로 켠 카메라(교통 미지정)에서도
        # VLM 호출·교통위험 판정·SOLAPI 알림이 조용히 돌고 있었던 것을
        # 실사용 점검 중 발견했다(docs/pending_tasks.md 2026-08-28 항목).
        # 실제로 교통위험 판정을 도는지는 반드시 이 값으로만 판단해야
        # 한다 — flood_enabled 주석과 같은 이유다.
        "traffic_enabled": bool(trow and trow.enabled),
    }
    # 침수 전용 설정(rainfall/river/rain/road_attrs)은 config 에 담아 뒀다.
    # 하위 도메인 키(crowd/traffic)는 아래에서 따로 채우므로 여기서는 뺀다.
    block.update({k: v for k, v in cfg.items() if k not in _SUB_DOMAIN_KEYS})

    # ★ 2026-08-21: 예전에는 crowd 만 분기로 처리해 도메인이 늘 때마다 이
    #   함수를 고쳐야 했다(그래서 road 는 아예 빠져 있었다). ROI_SHAPES 를
    #   근거로 도는 루프로 바꿔, 새 도메인은 ROI_SHAPES 에만 추가하면 된다.
    for dom in _SUB_DOMAINS:
        sub_row = cam.domain_row(dom)
        if not (sub_row and sub_row.enabled):
            continue
        sub_cfg = dict((sub_row.config or {}))
        roi = cam.roi_row(dom)
        shapes = (roi.shapes if roi else None) or {}
        # ROI 를 그린 정지영상의 해상도 — 실제 캡처 프레임과 크기가 다를 수
        # 있어(카메라 교체·스트림 프로파일 변경) 판정 쪽에서 좌표를 보정할
        # 때 필요하다(``common.roi.scale_polygons``). 없으면 보정을 건너뛴다
        # (2026-08-22, 교통·노면 ROI 판정 연동과 함께 추가).
        # 값을 아는 경우에만 싣는다 — 크기를 모르면 판정 쪽이 보정을
        # 건너뛰고 원본 좌표를 그대로 쓴다(scale_polygons 의 안전한 폴백).
        _rw = getattr(roi, "frame_width", None) if roi is not None else None
        _rh = getattr(roi, "frame_height", None) if roi is not None else None
        if _rw and _rh:
            sub_cfg["roi_frame_width"] = _rw
            sub_cfg["roi_frame_height"] = _rh
        # 설정된 것만 넘긴다 — 빈 목록을 넘기면 분석기가 「영역이 있는데 비었다」로
        # 오해해 아무것도 잡지 못한다.
        for key, *_ in ROI_SHAPES.get(dom, []):
            if shapes.get(key):
                sub_cfg[key] = shapes[key]
        block[dom] = sub_cfg
    return block


def continuous_blocks(db: Session, domain: str) -> list[dict]:
    """이 도메인에서 **상시 탐지로 지정된** 카메라를 블록 dict 목록으로.

    ``for_domain(continuous=True)`` + ``to_block_dict()`` 조합을 한 자리에
    모은 것이다 — 예전에는 ``service/main.py::_load_blocks()`` 안에만 있어서,
    상시 목록을 다시 계산해야 하는 다른 자리(``PipelineRunner`` 의 동적
    재구성)가 같은 조합을 또 써야 했다(2026-08-22 신설).
    """
    return [to_block_dict(c) for c in for_domain(db, domain, continuous=True)]


def continuous_blocks_any(db: Session, domains: tuple[str, ...]) -> list[dict]:
    """주어진 도메인 중 **하나라도** 상시 지정된 카메라를 블록 dict 목록으로.

    ⚠️ **왜 필요한가** (2026-08-23 실사용 중 발견) — 상시 처리 루프
    (``PipelineRunner``)의 대상 목록이 그동안 ``continuous_blocks(db,
    "flood")`` 하나만 봤다. 그래서 **침수는 미사용, 교통위험만 상시로 켠
    카메라는 처리 루프에 아예 올라가지 않았다** — 카메라 관리 화면에서
    교통위험 「상시」로 바꾸고 재기동해도, 그 카메라가 파이프라인에
    없으니 교통위험 실시간 관제 화면에 나타날 수 없었다(``/api/risk``가
    이 루프의 결과만 돌려주므로).

    카메라 하나가 여러 도메인에서 동시에 상시일 수 있어(예: 침수+교통
    둘 다 상시) 중복 없이 합친다. 변환은 ``to_block_dict()`` 한 번만
    한다 — 도메인마다 다시 변환하면 나중 것이 앞의 것을 덮어써 버린다.

    ⚠️ **주의**: 이 목록에 있다고 해서 침수 판정까지 도는 것은 아니다.
    실제로 침수를 도는지는 각 블록의 ``flood_enabled`` 플래그로만
    판단해야 한다(``runner.py`` 참고) — 교통만 상시인 카메라도 이
    함수를 거치면 ``to_block_dict()``가 여전히 flood domain_row 를
    조회하므로(꺼져 있어도 ROI 보관 원칙상 조회 자체는 됨),
    ``flood_enabled=False``로 표시되어 판정 실행을 막는다.
    """
    seen: dict[str, Camera] = {}
    for dom in domains:
        for c in for_domain(db, dom, continuous=True):
            seen.setdefault(c.id, c)
    return [to_block_dict(c) for c in seen.values()]


def to_roi_config_dict(cam: Camera) -> dict:
    """침수 ROI 를 기존 `common/roi.py` 가 읽는 형태로."""
    row = cam.roi_row(Domain.FLOOD.value)
    shapes = (row.shapes if row else None) or {}
    return {
        "camera_name": cam.id,
        "frame_width": row.frame_width if row else 0,
        "frame_height": row.frame_height if row else 0,
        "road_roi": shapes.get("road_roi") or [],
        "low_point_roi": shapes.get("low_point_roi") or [],
        "lane_threshold_line": shapes.get("lane_threshold_line") or [],
    }
