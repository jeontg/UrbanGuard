import numpy as np

from tot_dashboard.crowd.preemptive_action import PreemptiveAction
from tot_dashboard.crowd.semantic_risk_agent import SemanticRiskAgent


def _behavior(surge=1.0, dispersion=0.0, divergence=0.0):
    return {"n_tracks": 5, "mean_speed": 10.0, "surge": surge,
            "dispersion": dispersion, "divergence": divergence}


def test_normal_scene_scores_low_and_no_alert():
    agent = SemanticRiskAgent()
    risk = agent.assess({"density_pct": 10.0}, _behavior(), "정상")
    assert risk["risk_code"] == "NORMAL"

    action = PreemptiveAction(alert_min_sev=3)
    density_map = np.zeros((10, 10), dtype=np.float32)
    payload = action.act(0.0, 0, risk, density_map)
    assert payload["alert"] is False
    assert payload["hotspot"] is None


def test_high_divergence_and_surge_triggers_panic_dispersion_and_alerts():
    agent = SemanticRiskAgent()
    risk = agent.assess({"density_pct": 50.0}, _behavior(surge=1.5, divergence=8.0), "패닉")
    assert risk["risk_code"] == "PANIC_DISPERSION"
    assert risk["severity"] == 4

    action = PreemptiveAction(alert_min_sev=3)
    density_map = np.zeros((10, 10), dtype=np.float32)
    density_map[3, 7] = 5.0
    payload = action.act(1.5, 10, risk, density_map)
    assert payload["alert"] is True
    assert payload["hotspot"] == {"x": 7, "y": 3, "intensity": 5.0}
    assert len(action.alert_log) == 1


def test_high_density_alone_triggers_density_high():
    agent = SemanticRiskAgent(density_hi=40.0)
    risk = agent.assess({"density_pct": 55.0}, _behavior(), "혼잡")
    assert risk["risk_code"] == "CROWD_DENSITY_HIGH"
