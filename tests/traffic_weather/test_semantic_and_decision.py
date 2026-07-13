from tot_dashboard.models import (
    RiverState,
    RiverStatus,
    TrafficMetrics,
    TrafficState,
    WeatherIntensity,
    WeatherState,
)
from tot_dashboard.traffic_weather.agents.risk_decision import RiskDecisionAgent
from tot_dashboard.traffic_weather.agents.semantic_agent import SemanticAgent


def _weather(rain_mm_h: float, intensity: WeatherIntensity) -> WeatherState:
    return WeatherState(t_sec=0.0, rain_mm_h=rain_mm_h, intensity=intensity)


def _traffic(state: TrafficState, speed_drop: float = 0.0, stalled: int = 0,
             queue_len: int = 0) -> TrafficMetrics:
    return TrafficMetrics(t_sec=0.0, n_vehicles=5, mean_speed=50.0,
                          speed_drop=speed_drop, density=0.3,
                          queue_len=queue_len, stalled=stalled, state=state)


def test_no_rain_no_congestion_is_normal():
    agent = SemanticAgent()
    risk = agent.infer(_weather(0.0, WeatherIntensity.none), _traffic(TrafficState.free))
    assert risk.risk_code == "WIR_NORMAL"

    decision = RiskDecisionAgent().decide(risk, t_sec=0.0)
    assert decision.level.value == "관심"
    assert decision.alert is False


def test_heavy_rain_and_blocked_traffic_escalates_to_impassable():
    agent = SemanticAgent()
    risk = agent.infer(
        _weather(35.0, WeatherIntensity.very_heavy),
        _traffic(TrafficState.blocked, speed_drop=0.6, stalled=3),
    )
    assert risk.risk_code == "WIR_ROAD_IMPASSABLE"

    decision = RiskDecisionAgent().decide(risk, t_sec=10.0)
    assert decision.level.value == "심각"
    assert decision.alert is True


def test_river_danger_escalates_even_without_much_traffic_impact():
    agent = SemanticAgent()
    river = RiverState(level_m=4.5, status=RiverStatus.danger, ratio=1.1, station="test")
    risk = agent.infer(
        _weather(0.0, WeatherIntensity.none), _traffic(TrafficState.free),
        river=river,
    )
    assert risk.risk_code == "WIR_FLOOD_RISK"
    decision = RiskDecisionAgent().decide(risk, t_sec=0.0)
    assert decision.alert is True


def test_congestion_without_rain_is_not_weather_attributed():
    agent = SemanticAgent()
    risk = agent.infer(
        _weather(0.0, WeatherIntensity.none),
        _traffic(TrafficState.congested, speed_drop=0.6, stalled=1),
    )
    assert risk.risk_code == "WIR_NORMAL"
    assert "기상영향 아님" in risk.context
