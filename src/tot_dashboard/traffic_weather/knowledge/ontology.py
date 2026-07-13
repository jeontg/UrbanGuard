"""Rules/ontology for semantic inference (PoC).

Ported from flood3's ``knowledge/ontology.py``. Separates the
``WeatherImpactRisk`` classification scheme and rain-intensity thresholds out
as *data*, consumed by ``agents/semantic_agent.py``. A future graph-DB backend
(Neo4j/RDF) can replace this catalog while keeping ``SemanticAgent.infer()``'s
signature unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...models import RiskLevel, WeatherIntensity

# Rainfall (mm/h) -> intensity grade, ascending lower bounds.
RAIN_THRESHOLDS: list[tuple[float, WeatherIntensity]] = [
    (0.0, WeatherIntensity.none),
    (0.1, WeatherIntensity.light),
    (3.0, WeatherIntensity.moderate),
    (15.0, WeatherIntensity.heavy),
    (30.0, WeatherIntensity.very_heavy),
]


def rain_intensity(mm_h: float) -> WeatherIntensity:
    out = WeatherIntensity.none
    for lo, lvl in RAIN_THRESHOLDS:
        if mm_h >= lo:
            out = lvl
    return out


@dataclass(frozen=True)
class RiskDef:
    code: str
    name: str
    level: RiskLevel
    recommendation: str


RISK_CATALOG: dict[str, RiskDef] = {
    "WIR_NORMAL": RiskDef(
        "WIR_NORMAL", "정상", RiskLevel.interest,
        "정상 모니터링 유지"),
    "WIR_RAIN_ONSET": RiskDef(
        "WIR_RAIN_ONSET", "강우·노면젖음", RiskLevel.caution,
        "노면 미끄럼 주의 안내 / 모니터링 강화"),
    "WIR_RAIN_CONGESTION": RiskDef(
        "WIR_RAIN_CONGESTION", "강우 정체", RiskLevel.alert,
        "도로관리과 현장 점검 권고 / 정체 구간 공유"),
    "WIR_FLOOD_RISK": RiskDef(
        "WIR_FLOOD_RISK", "도로 침수위험", RiskLevel.alert,
        "순찰 강화·핫스팟 공유 / 배수시설 점검"),
    "WIR_ROAD_IMPASSABLE": RiskDef(
        "WIR_ROAD_IMPASSABLE", "도로 잠김·통행불가", RiskLevel.serious,
        "[긴급] 즉시 출동·우회 안내·통제센터 보고"),
}
