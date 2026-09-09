"""하천수위 전용 판정 — 순수 침수 신호만 (2026-08-21 신설).

flood/traffic 도메인 분리로, 예전에는 ``traffic_weather.agents.
semantic_agent.SemanticAgent``가 강우·교통·하천수위를 하나로 융합해
판정했다. 하천수위는 명백한 **직접 침수 신호**라 교통 신호(정체·정지차량)
를 절대 참조하지 않는 이 클래스로 분리했다.

반환값은 ``models.WeatherImpactRisk``를 그대로 재사용한다 — 필드 구조
(risk_code/risk_name/drivers/context/score)가 이미 도메인 무관이라
신규 모델을 만들 필요가 없었다. 이어서
``traffic_weather.agents.risk_decision.RiskDecisionAgent``(카탈로그
주입형으로 일반화됨)에 :data:`~.river_ontology.FLOOD_RIVER_CATALOG`를
넘겨 등급·알림여부(``Decision``)까지 얻는다 — traffic 쪽과 완전히
대칭적인 구조다.
"""
from __future__ import annotations

from ..models import RiverState, RiverStatus, WeatherImpactRisk
from .river_ontology import FLOOD_RIVER_CATALOG


class FloodRiverAgent:
    """하천수위만 보는 순수 flood 판정 — 교통 신호는 절대 참조하지 않는다."""

    def infer(self, river: RiverState | None, raining: bool = False,
              context: dict | None = None) -> WeatherImpactRisk:
        block = (context or {}).get("block", "대상 구역")

        if river is None or river.status == RiverStatus.normal:
            code = "FR_NORMAL"
        elif river.status == RiverStatus.advisory:
            code = "FR_RIVER_ADVISORY" if raining else "FR_NORMAL"
        elif river.status == RiverStatus.alert:
            code = "FR_RIVER_ALERT"
        else:  # RiverStatus.danger
            code = "FR_RIVER_DANGER" if raining else "FR_RIVER_ALERT"

        rdef = FLOOD_RIVER_CATALOG[code]
        drivers: list[str] = []
        if river is not None and river.status != RiverStatus.normal:
            drivers.append(f"하천수위 {river.status.value}({river.level_m:.1f}m)")
        if raining:
            drivers.append("강우 동반")

        if river is not None and river.status in (RiverStatus.alert, RiverStatus.danger):
            ctx = (f"{block}: 하천 수위 상승({river.status.value})"
                   + (" + 강우 동반 — 도로 침수 위험 추정" if raining
                      else " 관측 — 저지대 침수 주의"))
        else:
            ctx = f"{block}: 하천수위 정상"

        river_ratio = river.ratio if river is not None else 0.0
        score = round(min(1.0, river_ratio), 3)
        return WeatherImpactRisk(
            risk_code=code, risk_name=rdef.name,
            drivers=drivers or ["정상"], context=ctx, score=score)
