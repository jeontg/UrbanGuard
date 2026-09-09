"""Risk & Decision Agent — maps WeatherImpactRisk to the 4-level MOIS grade
plus a recommendation.

Ported from flood3's ``agents/risk_decision.py``. Inherits SAM's
``RiskAssessment``+``PreemptiveAction`` (``RECOMMEND``) pattern. Severity >= 2
(alert/경계 or worse) raises ``alert=True`` so the orchestrator notifies.

★ 2026-08-21 flood/traffic 도메인 분리: 카탈로그를 주입형으로 바꿨다 — 예전엔
``RISK_CATALOG``(WIR_*, 침수·교통 혼합) 하나만 봤지만, 이제 이 클래스는
``TRAFFIC_RISK_CATALOG``(교통)로도, ``flood.river_ontology.FLOOD_RIVER_CATALOG``
(침수·하천)로도 각각 독립적인 인스턴스를 만들 수 있다. 판정 로직(severity/
alert 계산) 자체는 그대로다.
"""
from __future__ import annotations

from ...models import LEVEL_SEVERITY, Decision, WeatherImpactRisk
from ..knowledge.ontology import RiskDef


class RiskDecisionAgent:
    def __init__(self, catalog: dict[str, RiskDef], alert_min_severity: int = 2):
        self.catalog = catalog
        # 2 = alert (경계) or worse triggers notification
        self.alert_min = alert_min_severity

    def decide(self, risk: WeatherImpactRisk, t_sec: float) -> Decision:
        rdef = self.catalog[risk.risk_code]
        severity = LEVEL_SEVERITY[rdef.level]
        return Decision(
            t_sec=round(t_sec, 2),
            risk_code=risk.risk_code,
            risk_name=risk.risk_name,
            level=rdef.level,
            severity=severity,
            score=risk.score,
            recommendation=rdef.recommendation,
            drivers=risk.drivers,
            context=risk.context,
            alert=severity >= self.alert_min)
