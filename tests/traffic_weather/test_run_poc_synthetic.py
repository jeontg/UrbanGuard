"""End-to-end regression check mirroring flood3's original ``run_poc.py``
synthetic-scenario console run: full 5-stage pipeline over a short synthetic
scene, crash-free, with sane values throughout.
"""
from tot_dashboard.common.notifier import AlertNotifier
from tot_dashboard.traffic_weather.agents.risk_decision import RiskDecisionAgent
from tot_dashboard.traffic_weather.agents.semantic_agent import SemanticAgent
from tot_dashboard.traffic_weather.agents.vlm_situation import VlmSituationAgent
from tot_dashboard.traffic_weather.perception.detection_source import SyntheticDetectionSource
from tot_dashboard.traffic_weather.perception.rainfall_provider import MockRainfallProvider
from tot_dashboard.traffic_weather.perception.traffic_tracker import TrafficBehaviorTracker
from tot_dashboard.traffic_weather.run_poc import _notify


def test_synthetic_scenario_runs_end_to_end_and_escalates_with_rain(monkeypatch):
    # dry-run notification path only needs recipients configured, not real
    # SOLAPI credentials (see AlertNotifier._ensure_ready)
    monkeypatch.setenv("NOTIFICATION_DRY_RUN", "true")
    monkeypatch.setenv("ALERT_RECIPIENTS", "01012345678")

    fps = 5.0
    rainfall = MockRainfallProvider(peak_mm_h=25.0, ramp_start=1.0, ramp_end=4.0)
    source = SyntheticDetectionSource(rainfall, fps=fps, duration_sec=8.0)
    tracker = TrafficBehaviorTracker(fps=fps, baseline_hint=source.base_speed)
    vlm = VlmSituationAgent(use_vlm=False)
    semantic = SemanticAgent()
    decider = RiskDecisionAgent()
    notifier = AlertNotifier()
    assert notifier.status()["dry_run"] is True

    levels_seen = set()
    frame_count = 0
    notified = 0
    for t, dets, _frame in source.frames():
        weather = rainfall.at(t)
        metrics = tracker.update(dets, t, frame_wh=source.frame_wh)
        situation = vlm.interpret(None, metrics, weather)
        assert situation["source"] == "rule-fallback"
        risk = semantic.infer(weather, metrics, situation, {"block": "test-block"})
        decision = decider.decide(risk, t)
        levels_seen.add(decision.level.value)
        if decision.alert:
            _notify(notifier, decision, "test-block")
            notified += 1
        frame_count += 1

    assert frame_count > 0
    # rain ramps up to 25mm/h and vehicles should slow -> at least one non-관심 level seen
    assert levels_seen - {"관심"}
    assert notified > 0
