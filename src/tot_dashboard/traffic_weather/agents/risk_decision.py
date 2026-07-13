"""Risk & Decision Agent — maps WeatherImpactRisk to the 4-level MOIS grade
plus a recommendation.

Ported from flood3's ``agents/risk_decision.py``. Inherits SAM's
``RiskAssessment``+``PreemptiveAction`` (``RECOMMEND``) pattern. Severity >= 2
(alert/경계 or worse) raises ``alert=True`` so the orchestrator notifies.
"""
from __future__ import annotations

from ...models import LEVEL_SEVERITY, Decision, WeatherImpactRisk
from ..knowledge.ontology import RISK_CATALOG


class RiskDecisionAgent:
    def __init__(self, alert_min_severity: int = 2):
        # 2 = alert (경계) or worse triggers notification
        self.alert_min = alert_min_severity

    def decide(self, risk: WeatherImpactRisk, t_sec: float) -> Decision:
        rdef = RISK_CATALOG[risk.risk_code]
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
