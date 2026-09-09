"""위험등급·위험유형 어휘 — Urban Ontology 1단계.

**왜 코드에 목록을 두는가.** 마이그레이션에만 넣으면 시험 DB 에 없다
(시험은 속도 때문에 ``create_all`` 을 쓴다). 오류 코드 사전(``error_catalog``)과
디지털 SOP(``sop``) 가 이미 쓰는 방식을 그대로 따른다 — 목록은 코드에,
심기는 :func:`seed_builtin` 에.

**덮어쓰지 않는다.** 기관이 등급 이름을 「위험」으로 바꿔 놓았는데 재기동마다
「심각」으로 되돌아가면 아무도 고치지 않는다.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import HazardType, LevelThreshold, RiskLevel

# --- 위험등급 ---------------------------------------------------------------
#
# label 은 **현재 코드가 반환하는 한글 문자열 그대로**다. ``core/calibration.py``
# 가 「관심·주의·경계·심각」을 돌려주고 있어서, 여기가 다르면 화면과 표가
# 어긋난다.
#
# code 를 영문으로 두는 이유 — 기관이 등급 이름을 바꿔도 code 는 그대로 두고
# label 만 고치면 된다. 한글을 키로 쓰면 이름을 못 바꾼다.
#
# seq 가 정렬과 비교의 유일한 기준이다. 이름으로 크기를 비교하면 기관이 이름을
# 바꾸는 순간 판정이 뒤집힌다.
DEFAULT_RISK_LEVELS: list[tuple[str, int, str, str, bool]] = [
    # code, seq, label, color, is_critical
    ("interest", 1, "관심", "#2f9e44", False),
    ("caution", 2, "주의", "#f59f00", False),
    ("alert", 3, "경계", "#e8590c", False),
    # 단독 발송이 허용되는 등급. roles.CRITICAL_LEVELS 와 같은 뜻이다.
    ("severe", 4, "심각", "#c92a2a", True),
]

# --- 등급의 성격 -------------------------------------------------------------
#
# ★ 왜 나누는가
#     노면은 「양호·관찰·보수 필요·긴급」인데 이것은 **위험등급이 아니라
#     정비 등급**이다. 「긴급」은 「지금 통제하라」가 아니라 **「빨리
#     보수하라」**다. 시간 축이 실시간이 아니라 주·월 단위다.
#
#     같은 자리에서 섞어 세면 상황판에서 침수 「심각」과 노면 「긴급」 중
#     **무엇이 더 급한지 알 수 없게** 된다. 그래서 **구간 숫자는 같은 표에
#     담아 화면에서 바꾸게 하되, 쓰이는 자리는 분리한다.**
KIND_RISK = "risk"
KIND_MAINTENANCE = "maintenance"

# 노면 정비 등급. **위험등급과 코드가 겹치지 않게** 접두사를 붙인다 —
# 겹치면 「긴급」과 「심각」이 같은 행을 가리키게 되어 통일한 셈이 된다.
DEFAULT_ROAD_LEVELS: list[tuple[str, int, str, str, bool]] = [
    # code, seq, label, color, is_critical(정비 등급에는 의미 없음 → 전부 False)
    ("road_good", 1, "양호", "#2f9e44", False),
    ("road_watch", 2, "관찰", "#f59f00", False),
    ("road_repair", 3, "보수 필요", "#e8590c", False),
    ("road_urgent", 4, "긴급", "#c92a2a", False),
]

# --- 위험유형 ---------------------------------------------------------------
#
# ``source='rfp'`` 는 경남 제안요청서 SFR-008 「재난 감지」에서 그대로 가져온
# 유형이다 — 제안서에서 근거를 댈 수 있다.
#
# ⚠️ ``detectable`` 이 False 인 것은 **어휘만 등록하고 모델은 없다**는 뜻이다.
# 이 구분이 없으면 화면이 「우리는 산불도 탐지한다」는 거짓말을 만든다.
# 부모가 목록에서 자식보다 앞에 와야 자기참조 FK 가 걸리지 않는다.
DEFAULT_HAZARD_TYPES: list[tuple[str, str, str | None, str, str, bool]] = [
    # code, domain, parent, label, source, detectable
    ("flood", "flood", None, "침수", "own", True),
    ("flood_underpass", "flood", "flood", "지하차도 침수", "rfp", True),
    ("flood_drainage", "flood", "flood", "배수로 침수", "rfp", True),
    # 하천 범람은 발주 요구지만 **우리 모델이 없다.**
    ("flood_river", "flood", "flood", "하천 범람", "rfp", False),
    # --- 교통 (2026-08-21 flood/traffic 도메인 분리로 신설) ---------------
    # ⚠️ 여기 code 는 「무엇이 발생했나(유형)」다. 판정 결과 상태를 나타내는
    #    TWR_*(traffic_weather.knowledge.ontology.TRAFFIC_RISK_CATALOG)와는
    #    **다른 네임스페이스**다 — 두 축을 같은 칸에 넣으려다 겪은 문제
    #    (docs/202608210801 5절)는 여전히 유효해 이 목록 자체를 합치지는
    #    않는다. 다만 **판정축 → 유형축 단방향 번역표**는 뒀다
    #    (2026-08-26, `ontology.TWR_TO_HAZARD_TYPE`) — 매핑이 아예 없으면
    #    이벤트가 전부 대분류 "traffic" 로 뭉개져 유형별 통계·SOP 연결이
    #    안 됐다(`docs/202608260842/` 참고).
    ("traffic", "traffic", None, "교통", "own", True),
    ("traffic_rain_congestion", "traffic", "traffic", "강우 정체", "own", True),
    ("traffic_stalled_vehicle", "traffic", "traffic", "정지·고착 차량", "own", True),
    ("traffic_queue_delay", "traffic", "traffic", "대기열 지연", "own", True),
    ("traffic_impassable", "traffic", "traffic", "통행 불가", "own", True),
    # --- 교통 돌발상황 확장 (2026-08-26) -----------------------------------
    # 전문 AID 솔루션 비교(위 문서)에서 확인한 격차를 메운다. 「사고」가
    # 아니라 「사고 의심」인 이유 — 사고 라벨로 학습한 모델이 없고, 정지차량
    # 군집이라는 **대리지표**로만 판정한다(traffic_weather/perception/
    # incident_events.py). 이름에서부터 확정을 피한다.
    ("traffic_pedestrian", "traffic", "traffic", "보행자 도로 진입", "own", True),
    ("traffic_wrongway", "traffic", "traffic", "역주행 의심", "own", True),
    ("traffic_accident", "traffic", "traffic", "사고 의심", "own", True),
    # 낙하물·화재연기는 COCO에 없는 클래스라 자체 학습 없이는 탐지할 수
    # 없다(D:\dev-PoC_DATA\07_학습데이터_교통 폴더는 있으나 비어 있음.
    # 2026-08-26 Phase 7 — 수집·라벨링·학습 진입점 스크립트 4종은 준비했지만
    # 데이터 자체가 아직 없다. docs/202608261606/traffic_incident_labeling_
    # spec.md 참고) — detectable=False 로 심어 화면에 "탐지한다"고 뜨지
    # 않게 하면서도 미탐 신고(S-07) 어휘로는 미리 쓸 수 있게 한다. 모델이
    # 생기면 True로.
    ("traffic_debris", "traffic", "traffic", "낙하물", "own", False),
    ("traffic_fire_smoke", "traffic", "traffic", "화재·연기", "own", False),
    # 안개·결빙은 여기 넣지 않았다 — 영상이 아니라 외부 기상 센서/API에
    # 의존하기로 확인됐다(기상청 도로위험기상정보는 재정고속도로 전용망이라
    # 우리 지자체 도심 지점과 겹치지 않음, 2026-08-26 조사). 데이터 출처가
    # 정해지기 전에는 detectable 을 True/False 어느 쪽으로도 정직하게 못
    # 정한다 — `traffic_weather/perception/rainfall_provider.py` 머리말의
    # 확장 지점 안내 참고.
    ("crowd", "crowd", None, "인파", "own", True),
    ("crowd_density", "crowd", "crowd", "인파 밀집", "own", True),
    ("crowd_loitering", "crowd", "crowd", "배회", "own", True),
    ("road", "road", None, "노면", "own", True),
    ("road_pothole", "road", "road", "노면 파손", "own", True),
    # 도메인이 비어 있다 — 우리 3개 도메인 어디에도 속하지 않는다.
    ("wildfire", "", None, "산불 확산", "rfp", False),
    ("typhoon_damage", "", None, "태풍 피해", "rfp", False),
]


# --- 도메인별 등급 구간 -----------------------------------------------------
#
# ``min_value`` 이상이면 그 등급이다. 숫자의 출처는 전부 외부 기준이며,
# ``source_note`` 로 화면에 함께 띄운다 — 기관이 바꾸더라도 **원래 근거가
# 무엇이었는지는 남아야** 한다.
#
# ⚠️ 침수 어휘를 4등급으로 통일했다. 이전에는 「통제 권고·위험·주의·관심」이라
# 인파(관심·주의·경계·심각)와 말이 달랐다. 같은 상황판에 서로 다른 등급 이름이
# 뜨면 관제요원이 어느 쪽이 더 급한지 알 수 없다.
#
# **여기에 없는 두 어휘는 통일하지 않기로 확정했다(2026-08-19).**
#
# - 노면 「양호·관찰·보수 필요·긴급」(:func:`~.calibration.road_level`) —
#   위험등급이 아니라 **정비 등급**이다. 「지금 위험하다」가 아니라 「언제까지
#   고쳐야 한다」라서 같은 척도에 올리면 뜻이 엉킨다
# - 침수 단독 파이프라인 5단계(:data:`~..flood.alert_engine.LEVEL_NAME`) —
#   상황판 이벤트가 아니라 **분석 영상 오버레이**의 등급이다
# - **교통(2026-08-21 신설)** — 등급 어휘는 침수·인파와 같은 4단계를 쓰지만
#   (그래서 ``kind`` 를 나눌 필요가 없다), 판정이 **단일 스칼라 임계값
#   비교가 아니라 강우량×속도저하×정지차량 다변량 조합**이다. 이 표는
#   ``(도메인, 등급, 최소값, 단위)`` 구조라 변수를 하나만 담을 수 있어,
#   억지로 넣으면 화면에 뜨는 「근거 값」이 실제 판정 로직과 달라진다.
#   기관이 임계값을 직접 조정해야 할 필요가 생기면 그때 다변량용 표를
#   따로 설계한다(docs/202608210801 5절)
#
# 그래서 이 목록은 **상황판 이벤트 등급을 만드는 도메인만** 담는다.
#
# (도메인, 등급코드, 최소값, 단위, 근거)
DEFAULT_THRESHOLDS: list[tuple[str, str, float, str, str]] = [
    ("flood", "caution", 5.0, "cm", "행안부 지하차도 통제 기준(15→5cm 강화)"),
    ("flood", "alert", 15.0, "cm", "차량 접지력 상실 시작 (NWS/FEMA)"),
    ("flood", "severe", 30.0, "cm", "소형차 부유 시작 (NWS/FEMA)"),
    ("crowd", "caution", 3.0, "명/㎡", "혼잡 시작"),
    ("crowd", "alert", 4.0, "명/㎡", "영국 이동 대기열 한계"),
    ("crowd", "severe", 5.0, "명/㎡", "국제 압사 임계"),
    # 노면 — **정비 등급**이라 위 둘과 성격이 다르다(KIND_MAINTENANCE 참고).
    # 단위는 「100m 당 손상 건수」다. 구간 길이로 보정한 값이라, 길이를 안
    # 넣은 지점은 판정 자체가 나오지 않는다(calibration 참고).
    ("road", "road_watch", 1.0, "건/100m", "구간 보정 후 손상이 보이기 시작"),
    ("road", "road_repair", 3.0, "건/100m", "보수 계획 수립 권고"),
    ("road", "road_urgent", 6.0, "건/100m", "즉시 보수 대상"),
]

# 「관심」은 구간의 바닥이라 표에 넣지 않는다 — 어느 등급에도 못 미치면 관심이다.
# 노면도 같다 — 어느 구간에도 못 미치면 「양호」다.
BASE_LEVEL = "interest"
BASE_ROAD_LEVEL = "road_good"

# 도메인별 바닥 등급. 구간 어디에도 못 미쳤을 때 무엇으로 볼 것인가.
# 교통은 침수·인파와 같은 4단계 위험등급을 쓴다(위 DEFAULT_THRESHOLDS 주석 참고).
BASE_LEVEL_BY_DOMAIN = {"flood": BASE_LEVEL, "crowd": BASE_LEVEL,
                        "traffic": BASE_LEVEL, "road": BASE_ROAD_LEVEL}


def seed_builtin(db: Session) -> tuple[int, int]:
    """어휘를 심는다. 이미 있으면 건드리지 않는다. ``(등급 수, 유형 수)``."""
    have_lv = {r.code for r in db.scalars(select(RiskLevel)).all()}
    lv = 0
    for rows, kind in ((DEFAULT_RISK_LEVELS, KIND_RISK),
                       (DEFAULT_ROAD_LEVELS, KIND_MAINTENANCE)):
        for code, seq, label, color, crit in rows:
            if code in have_lv:
                continue
            db.add(RiskLevel(code=code, kind=kind, seq=seq, label=label,
                             color=color, is_critical=crit))
            lv += 1

    have_ht = {r.code for r in db.scalars(select(HazardType)).all()}
    ht = 0
    for code, domain, parent, label, source, detectable in DEFAULT_HAZARD_TYPES:
        if code in have_ht:
            continue
        db.add(HazardType(code=code, domain=domain, parent_code=parent,
                          label=label, source=source, detectable=detectable))
        ht += 1

    have_th = {(r.domain, r.level_code)
               for r in db.scalars(select(LevelThreshold)).all()}
    for domain, code, minv, unit, note in DEFAULT_THRESHOLDS:
        if (domain, code) in have_th:
            continue
        db.add(LevelThreshold(domain=domain, level_code=code, min_value=minv,
                              unit=unit, source_note=note, updated_by="system"))

    if lv or ht or not have_th:
        db.flush()
    return lv, ht


# --- 등급 구간 조회·저장 ----------------------------------------------------


def kind_of_domain(domain: str) -> str:
    """이 도메인이 쓰는 등급의 성격.

    노면만 **정비 등급**이다. 나머지는 위험등급이다.
    """
    return (KIND_MAINTENANCE if (domain or "").strip() == "road"
            else KIND_RISK)


def order_for_domain(db: Session, domain: str) -> dict[str, int]:
    """이 도메인 등급의 ``{code: seq}``.

    ⚠️ :func:`level_order` 를 그대로 쓰면 안 된다. 그쪽은 위험등급만 담아서
    **노면 구간이 통째로 버려진다** — 실제로 그렇게 만들었다가 잡았다.
    """
    return {r.code: r.seq for r in db.scalars(select(RiskLevel).where(
        RiskLevel.kind == kind_of_domain(domain))).all()}


def thresholds(db: Session, domain: str) -> list[tuple[str, float, str]]:
    """``[(level_code, min_value, unit), …]`` **높은 등급부터**.

    비교는 위에서 내려오며 한다 — 낮은 것부터 보면 「5cm 이상」에 걸려
    30cm 도 「주의」가 된다.
    """
    order = order_for_domain(db, domain)
    rows = db.scalars(select(LevelThreshold)
                      .where(LevelThreshold.domain == domain)).all()
    got = [(r.level_code, float(r.min_value), r.unit) for r in rows
           if r.level_code in order]
    got.sort(key=lambda t: order[t[0]], reverse=True)
    return got


def threshold_rows(db: Session, domain: str) -> list[dict]:
    """화면용 — 등급 이름·근거까지 붙여서 낮은 등급부터."""
    levels = {r.code: r for r in db.scalars(select(RiskLevel)).all()}
    rows = db.scalars(select(LevelThreshold)
                      .where(LevelThreshold.domain == domain)).all()
    out = []
    for r in rows:
        lv = levels.get(r.level_code)
        out.append({"id": r.id, "level_code": r.level_code,
                    "label": lv.label if lv else r.level_code,
                    "seq": lv.seq if lv else 0,
                    "color": lv.color if lv else "",
                    "min_value": r.min_value, "unit": r.unit,
                    "source_note": r.source_note})
    out.sort(key=lambda d: d["seq"])
    return out


def label_of(db: Session, code: str) -> str:
    """등급 코드 → 화면에 쓸 이름. 모르면 코드를 그대로 돌려준다."""
    r = db.get(RiskLevel, code)
    return r.label if r else code


def active_levels(db: Session, kind: str = KIND_RISK) -> list[RiskLevel]:
    """쓰이는 등급을 낮은 것부터. 화면 정렬과 선택 목록에 쓴다.

    ⚠️ **기본은 위험등급만**이다. 노면 정비 등급(`maintenance`)까지 섞어
    주면 상황판 선택 목록에 「보수 필요」가 뜬다 — 위험 상황에 고를 수 있는
    값이 아니다. 정비 등급이 필요하면 `kind` 를 명시해서 부른다.
    """
    return list(db.scalars(
        select(RiskLevel).where(RiskLevel.is_active.is_(True),
                                RiskLevel.kind == kind)
        .order_by(RiskLevel.seq)))


def level_order(db: Session) -> dict[str, int]:
    """``{code: seq}``. 등급 비교에 쓴다 — **이름이 아니라 seq 로 비교**한다.

    ⚠️ 위험등급만 담는다. 정비 등급은 seq 가 1~4 로 겹치는데 **비교 대상이
    아니다** — 섞으면 「보수 필요(3)」와 「경계(3)」가 같은 급으로 읽힌다.
    """
    return {r.code: r.seq for r in db.scalars(
        select(RiskLevel).where(RiskLevel.kind == KIND_RISK)).all()}


def rank(db: Session, label_or_code: str | None) -> int:
    """등급의 순위(1부터). 모르는 값이면 0.

    **이름 비교를 없애기 위한 함수다.** 기관이 「심각」을 「위험」으로 바꾸면
    이름 비교는 조용히 틀리지만, seq 비교는 그대로 맞는다.
    """
    v = (label_or_code or "").strip()
    if not v:
        return 0
    # ⚠️ 위험등급 안에서만 찾는다. 노면 「긴급」을 여기서 4로 돌려주면
    #    **정비 대상이 침수 「심각」과 같은 급으로 경보**된다.
    for r in db.scalars(select(RiskLevel).where(
            RiskLevel.kind == KIND_RISK)).all():
        if v in (r.code, r.label):
            return r.seq
    return 0


def critical_labels(db: Session) -> set[str]:
    """단독 발송이 허용되는 등급의 **code 와 label 둘 다**.

    ``roles.CRITICAL_LEVELS`` 하드코딩을 대체한다. code 와 label 을 함께 주는
    이유 — 이벤트에 저장된 값이 지금은 한글 label 이고, 앞으로 code 로 옮겨 갈
    수 있어서 **양쪽 다 받아 줘야** 이행 중에 구멍이 안 생긴다.

    ⚠️ 표가 비어 있으면 **빈 집합**을 준다. 호출부가 이걸 「전부 허용」으로
    읽으면 안 되고, 하드코딩 기본값으로 되돌아가야 한다(``roles`` 참고).
    """
    out: set[str] = set()
    for r in db.scalars(select(RiskLevel).where(
            RiskLevel.is_critical.is_(True),
            RiskLevel.kind == KIND_RISK)).all():
        out.add(r.code)
        if r.label:
            out.add(r.label)
    return out


def detectable_types(db: Session) -> list[HazardType]:
    """실제로 탐지 가능한 유형만.

    화면에서 「우리가 무엇을 볼 수 있는가」를 보여 줄 때 이걸 쓴다. 전체 목록을
    쓰면 산불·태풍까지 탐지하는 것처럼 보인다.
    """
    return list(db.scalars(
        select(HazardType)
        .where(HazardType.detectable.is_(True), HazardType.is_active.is_(True))
        .order_by(HazardType.domain, HazardType.code)))


# --- 도메인 → 기본 위험유형 --------------------------------------------------
#
# ★ 왜 필요한가
#     ``events.hazard_type_code`` 를 **채우는 곳이 없어** 늘 비어 있었다
#     (2026-08-19 확인). 유형 어휘를 만들어 두고 이벤트가 그걸 안 가리키면,
#     유형 기준 통계·필터·SOP 연결이 전부 빈손이 된다.
#
# ⚠️ **도메인에서 도출할 수 있는 것은 「대분류」뿐이다.**
#     「지하차도 침수」인지 「배수로 침수」인지는 도메인만으로 알 수 없다.
#     탐지기가 그것까지 알려 주면 그때 세분류를 넣고, 모르면 대분류로 둔다.
#     **모르는 것을 아는 척해서 세분류를 찍으면 안 된다** — 그 값으로 SOP 가
#     갈리는데, 틀린 절차를 안내하는 것은 절차가 없는 것보다 나쁘다.
BASE_HAZARD_BY_DOMAIN = {
    "flood": "flood",
    "traffic": "traffic",
    "crowd": "crowd",
    "road": "road",
}

# 코드가 우리 어휘에 있는 것인지 확인할 때 쓴다. DB 를 매번 조회하지 않는다 —
# 탐지 한 번마다 질의를 더하면 상시 탐지에서 비용이 쌓인다.
KNOWN_HAZARD_CODES = frozenset(c for c, *_ in DEFAULT_HAZARD_TYPES)


def base_hazard_for(domain: str) -> str:
    """이 도메인의 **대분류** 위험유형 코드. 모르면 빈 문자열."""
    return BASE_HAZARD_BY_DOMAIN.get((domain or "").strip(), "")


def normalize_hazard_code(code: str, domain: str = "") -> str:
    """넘어온 유형 코드를 쓸 수 있는 값으로 만든다.

    - 비어 있으면 도메인에서 **대분류**를 도출한다
    - 우리 어휘에 없는 코드면 **버리고** 대분류로 떨어뜨린다

    ⚠️ 모르는 코드를 그대로 저장하면 안 된다. 나중에 유형별로 묶을 때
    **어디에도 속하지 않는 이벤트**가 되어 조용히 통계에서 빠진다.
    """
    code = (code or "").strip()
    if code and code in KNOWN_HAZARD_CODES:
        return code
    return base_hazard_for(domain)
