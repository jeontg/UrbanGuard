"""이벤트 상세 화면 — 「발생 원인」 설명 (2026-09-03 신설).

## 왜 필요한가

사용자 요청(원문): "이벤트 목록에서 이벤트마다 상세보기가 있는데
상세보기에서 이벤트가 왜 발생했는지 상세하게 알려주세요." 지금
이벤트 상세(S-03, ``event_detail.html``)의 「근거」 카드는 ``Event.detail``
(JSONB)을 그냥 키-값으로 나열만 한다 — 숫자는 보이지만 "그래서 왜
이 등급인가"를 사람이 다시 조합해야 했다.

## 왜 새 컬럼·마이그레이션이 없는가

이미 저장된 값(``Event.detail``·``hazard_type_code``·``level``)과 어휘표
(``vocabulary.threshold_rows``·``HazardType``)를 **조회 시점에 조합**해
문장으로 만든다. 그래서 지금까지 쌓인 과거 이벤트에도 즉시 똑같이
적용된다 — 새로 탐지되는 것만 설명이 붙는 게 아니다.

## 왜 ``detail`` 키를 방어적으로 찾는가

``detail`` 딕셔너리는 고정 스키마가 아니다 — ``service/event_sync.py``의
호출부가 도메인·이벤트 종류(침수/교통위험 TWR/교통 돌발상황/인파 밀집/
인파 배회/노면 손상/주민제보 전환)마다 서로 다른 한글 키를 쓴다(실측
확인, 2026-09-03). 모르는 모양이 오면 예외를 던지지 않고 조용히
일반 설명으로 물러난다 — 이 카드 하나 때문에 이벤트 상세 화면 전체가
깨지면 안 된다.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import vocabulary
from .models import Event, HazardType


def _hazard_label(db: Session, code: str | None) -> str | None:
    if not code:
        return None
    row = db.get(HazardType, code)
    return row.label if row else None


def _matched_threshold(rows: list[dict], level: str) -> dict | None:
    """이 이벤트의 등급을 만든 기준 행(있으면) — 등급 이름으로 맞춘다."""
    for r in rows:
        if r["label"] == level:
            return r
    return None


def _fmt_ratio(v) -> str | None:
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return None


def _flood_basis(d: dict) -> str | None:
    parts = []
    depth = d.get("추정 침수심(cm)")
    if depth is not None:
        parts.append(f"추정 침수심 {depth}cm")
    ratio = _fmt_ratio(d.get("물 비율"))
    if ratio is not None:
        parts.append(f"화면 내 물 비율 {ratio}")
    vehicles = d.get("바퀴 침수 차량")
    if vehicles:
        parts.append(f"바퀴가 잠긴 차량 {vehicles}대")
    persons = d.get("위험 보행자")
    if persons:
        parts.append(f"위험 보행자 {persons}명")
    return ", ".join(parts) if parts else None


def _traffic_basis(d: dict) -> str | None:
    # 돌발상황(보행자·역주행·사고 의심) 경로 — "근거"에 이미 사람이 읽을
    # 문장이 들어있다(traffic_weather/perception/incident_events.py).
    if d.get("근거"):
        return str(d["근거"])
    # TWR(강우·정체) 경로.
    parts = []
    rain = d.get("강수(mm/h)")
    if rain is not None:
        inten = d.get("강수 강도")
        parts.append(f"강수 {rain}mm/h" + (f"({inten})" if inten else ""))
    speed = d.get("평균속도(km/h)")
    if speed is not None:
        parts.append(f"평균속도 {speed}km/h")
    n = d.get("차량 수")
    if n is not None:
        parts.append(f"차량 {n}대")
    return ", ".join(parts) if parts else None


def _crowd_basis(d: dict) -> str | None:
    parts = []
    if d.get("근거"):
        parts.append(str(d["근거"]))
    person = d.get("현재 인원")
    if person is not None:
        parts.append(f"인원 {person}명")
    density = d.get("밀집지수")
    if density is not None:
        parts.append(f"밀집지수 {density}")
    kinds = d.get("탐지 유형")
    if kinds:
        parts.append(f"탐지 유형 {kinds}")
    return ", ".join(parts) if parts else None


def _road_basis(d: dict) -> str | None:
    kinds = d.get("손상 유형")
    count = d.get("탐지 건수")
    if kinds or count is not None:
        bits = []
        if kinds:
            bits.append(str(kinds))
        if count is not None:
            bits.append(f"{count}건")
        return " ".join(bits)
    # 주민 제보를 이벤트로 전환한 경우(routes_reports.py).
    if d.get("설명"):
        reporter = d.get("제보자")
        tail = f"(제보자 {reporter})" if reporter else ""
        return f"주민 제보: {d['설명']}{tail}"
    return None


_BASIS_BY_DOMAIN = {
    "flood": _flood_basis,
    "traffic": _traffic_basis,
    "crowd": _crowd_basis,
    "road": _road_basis,
}


def explain(db: Session, ev: Event) -> dict:
    """이 이벤트가 왜 발생했는지 사람이 읽을 설명을 만든다.

    반환값은 항상 이 모양이다(어떤 도메인·상황이든 예외 없이):
    ``{"headline": str, "hazard_label": str|None,
      "threshold_rows": list[dict], "matched_threshold": dict|None}``
    """
    try:
        d = ev.detail or {}
        hazard_label = _hazard_label(db, ev.hazard_type_code)
        basis_fn = _BASIS_BY_DOMAIN.get(ev.domain)
        basis = basis_fn(d) if basis_fn else None

        th_rows = vocabulary.threshold_rows(db, ev.domain)
        matched = _matched_threshold(th_rows, ev.level)

        what = hazard_label or ev.event_type or "이상 상황"
        if basis:
            headline = f"{what}({basis})이(가) 감지되어 「{ev.level}」 등급으로 판정됐습니다."
        else:
            headline = f"{what}이(가) 감지되어 「{ev.level}」 등급으로 판정됐습니다."
        if matched:
            headline += (f" 판정 기준: {matched['label']} {matched['min_value']:g}"
                        f"{matched['unit']} 이상"
                        + (f"({matched['source_note']})" if matched['source_note'] else "")
                        + ".")

        return {"headline": headline, "hazard_label": hazard_label,
                "threshold_rows": th_rows, "matched_threshold": matched}
    except Exception:  # noqa: BLE001 — 설명 카드 하나 때문에 상세 화면이 깨지면 안 된다.
        return {"headline": f"「{ev.level}」 등급으로 판정됐습니다 — 상세 근거는 아래 표를 참고하십시오.",
                "hazard_label": None, "threshold_rows": [], "matched_threshold": None}
