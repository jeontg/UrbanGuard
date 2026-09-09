"""Rules/ontology for semantic inference (PoC).

Ported from flood3's ``knowledge/ontology.py``. Separates the
``WeatherImpactRisk`` classification scheme and rain-intensity thresholds out
as *data*, consumed by ``agents/semantic_agent.py``. A future graph-DB backend
(Neo4j/RDF) can replace this catalog while keeping ``SemanticAgent.infer()``'s
signature unchanged.

★ 2026-08-21 flood/traffic 도메인 분리: 옛 ``RISK_CATALOG``(WIR_*)는 강우·
정체·하천수위가 섞인 혼합 판정이었다. 이제 **순수 교통·기상** 판정만 이
카탈로그(``TRAFFIC_RISK_CATALOG``, ``TWR_*``)에 남는다. 하천수위(직접 침수
신호)는 ``flood/river_ontology.py``의 ``FLOOD_RIVER_CATALOG``(``FR_*``)로
완전히 분리했다 — 두 카탈로그는 코드값 네임스페이스가 겹치지 않아, 강한
강우+정지차량 다발+하천경계가 동시에 관측되면 두 도메인이 각각 독립적으로
자기 코드를 낼 수 있다(docs/202608210801 참고).
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


TRAFFIC_RISK_CATALOG: dict[str, RiskDef] = {
    "TWR_NORMAL": RiskDef(
        "TWR_NORMAL", "정상", RiskLevel.interest,
        "정상 모니터링 유지"),
    "TWR_RAIN_ONSET": RiskDef(
        "TWR_RAIN_ONSET", "강우·노면젖음", RiskLevel.caution,
        "노면 미끄럼 주의 안내 / 모니터링 강화"),
    "TWR_RAIN_CONGESTION": RiskDef(
        "TWR_RAIN_CONGESTION", "강우 정체", RiskLevel.alert,
        "도로관리과 현장 점검 권고 / 정체 구간 공유"),
    # 구 WIR_FLOOD_RISK의 교통측 절반 — 정지차량 다발(교통 행동)만으로 판정,
    # 하천수위는 이제 전혀 참조하지 않는다.
    "TWR_SEVERE_CONGESTION": RiskDef(
        "TWR_SEVERE_CONGESTION", "강우 중 정지차량 다발", RiskLevel.alert,
        "정체 구간 공유 / 우회 안내 검토"),
    # 구 WIR_ROAD_IMPASSABLE의 교통측 절반.
    "TWR_SEVERE_GRIDLOCK": RiskDef(
        "TWR_SEVERE_GRIDLOCK", "심각한 교통마비", RiskLevel.serious,
        "[긴급] 우회 안내·통제센터 보고"),
}


# ★ 2026-08-26 — TWR_* → traffic_* 위험유형 단방향 매핑.
#
# 두 네임스페이스는 여전히 **합치지 않는다**(위 2026-08-21 설명 그대로 —
# 판정 상태 축과 위험유형 축은 성격이 다르다). 이 표는 그 둘 사이의
# **번역**일 뿐이다 — TWR_* 가 「지금 상태가 뭔가」를 답한다면, 이 표를 거친
# traffic_* 코드는 「그 상태가 어떤 유형으로 기록되는가」를 답한다.
#
# 이게 없으면 ``event_sync._sync_traffic()``이 만드는 이벤트가 전부 대분류
# ``"traffic"`` 로 뭉개져(위험유형 어휘에 이미 있던 4종이 한 번도 채워진
# 적이 없었다) 유형별 통계·피드백·SOP 연결이 불가능했다
# (`docs/202608260842/traffic_risk_types_vs_professional_solutions.md`).
#
# ``traffic_queue_delay``는 대응하는 TWR_* 코드가 없다 — **일부러
# 매핑하지 않는다.** 없는 대응을 억지로 만들면 「대기열 지연」 통계에
# 실제로는 강우 정체였던 사례가 섞인다.
TWR_TO_HAZARD_TYPE: dict[str, str] = {
    "TWR_NORMAL": "",                          # 관심 등급 — 애초에 이벤트가 안 됨
    "TWR_RAIN_ONSET": "traffic_rain_congestion",
    "TWR_RAIN_CONGESTION": "traffic_rain_congestion",
    "TWR_SEVERE_CONGESTION": "traffic_stalled_vehicle",
    "TWR_SEVERE_GRIDLOCK": "traffic_impassable",
}
