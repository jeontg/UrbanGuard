"""Semantic reasoning — rule/ontology fusion of weather x traffic x river.

Ported from flood3's ``agents/semantic_agent.py``. The key idea is *semantic
fusion*: identical congestion is classified as weather-driven risk if it's
raining, and as ordinary congestion otherwise. A high river level (alert/
danger) is a direct flood signal that can raise the risk further.

PoC infers via ``knowledge/ontology.py``'s catalog + the rules below. A future
graph-DB backend can replace this while keeping ``infer()``'s signature.
"""
from __future__ import annotations

from ...models import (
    LEVEL_SEVERITY,
    RiverState,
    RiverStatus,
    TrafficMetrics,
    TrafficState,
    WeatherImpactRisk,
    WeatherIntensity,
    WeatherState,
)
from ..knowledge.ontology import RISK_CATALOG

_HEAVY = (WeatherIntensity.heavy, WeatherIntensity.very_heavy)
_RAINY_MID = (WeatherIntensity.moderate, WeatherIntensity.heavy, WeatherIntensity.very_heavy)


def _sev(code: str) -> int:
    return LEVEL_SEVERITY[RISK_CATALOG[code].level]


class SemanticAgent:
    """Rule/ontology-based fusion inference. ``infer()`` stays stable even if
    the backend is later replaced by a graph DB."""

    def infer(self, weather: WeatherState, traffic: TrafficMetrics,
              situation: dict | None = None, context: dict | None = None,
              river: RiverState | None = None) -> WeatherImpactRisk:
        rain = weather.rain_mm_h
        inten = weather.intensity
        drop = traffic.speed_drop
        stalled = traffic.stalled
        queue = traffic.queue_len
        raining = inten != WeatherIntensity.none
        block = (context or {}).get("block", "대상 구역")

        drivers: list[str] = []
        if raining:
            drivers.append(f"강수 {inten.value}({rain:.0f}mm/h)")
        if drop > 0.2:
            drivers.append(f"평균속도 {drop * 100:.0f}% 감소")
        if queue >= 3:
            drivers.append(f"정체 대기열 {queue}대")
        if stalled >= 1:
            drivers.append(f"정지차량 {stalled}대")
        if river is not None and river.status != RiverStatus.normal:
            drivers.append(f"하천수위 {river.status.value}({river.level_m:.1f}m)")

        # ── traffic-based candidate ──
        if raining and inten in _HEAVY and traffic.state == TrafficState.blocked and stalled >= 2:
            code = "WIR_ROAD_IMPASSABLE"
        elif raining and inten in _RAINY_MID and stalled >= 2:
            code = "WIR_FLOOD_RISK"
        elif raining and drop > 0.4 and traffic.state == TrafficState.congested:
            code = "WIR_RAIN_CONGESTION"
        elif raining and drop > 0.2:
            code = "WIR_RAIN_CONGESTION" if traffic.state == TrafficState.congested else "WIR_RAIN_ONSET"
        elif raining:
            code = "WIR_RAIN_ONSET"
        else:
            code = "WIR_NORMAL"

        # ── river-based candidate (direct flood signal) ──
        river_code = None
        if river is not None:
            if river.status == RiverStatus.danger:
                river_code = "WIR_ROAD_IMPASSABLE" if raining else "WIR_FLOOD_RISK"
            elif river.status == RiverStatus.alert:
                river_code = "WIR_FLOOD_RISK"
            elif river.status == RiverStatus.advisory and raining:
                river_code = "WIR_RAIN_ONSET"

        # ── fuse: take the higher risk ──
        final = code
        if river_code and _sev(river_code) > _sev(final):
            final = river_code

        # ── semantic context sentence ──
        river_hi = river is not None and river.status in (RiverStatus.alert, RiverStatus.danger)
        if river_hi and raining:
            ctx = f"{block}: 강우 + 하천 수위 상승({river.status.value}) — 도로 침수 위험 추정"
        elif raining and drop > 0.2:
            ctx = f"{block}: 강우와 교통 정체가 동시 관측됨 — 강수 영향으로 추정"
        elif river_hi:
            ctx = f"{block}: 하천 수위 상승({river.status.value}) 관측 — 저지대 침수 주의"
        elif drop > 0.2:
            ctx = f"{block}: 강수 없음 — 기상영향 아님(일반 정체 가능성)"
        elif raining:
            ctx = f"{block}: 강우 관측, 교통 영향은 경미"
        else:
            ctx = f"{block}: 특이사항 없음"

        rdef = RISK_CATALOG[final]
        river_ratio = river.ratio if river is not None else 0.0
        score = min(1.0, 0.35 * min(rain / 30.0, 1.0) + 0.30 * drop
                    + 0.15 * min(stalled / 5.0, 1.0) + 0.20 * min(river_ratio, 1.0))
        return WeatherImpactRisk(
            risk_code=final, risk_name=rdef.name,
            drivers=drivers or ["정상"], context=ctx, score=round(score, 3))
