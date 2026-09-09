from tot_dashboard.models import TrafficMetrics, TrafficState, WeatherIntensity, WeatherState
from tot_dashboard.traffic_weather.agents.risk_decision import RiskDecisionAgent
from tot_dashboard.traffic_weather.agents.semantic_agent import SemanticAgent
from tot_dashboard.traffic_weather.knowledge.ontology import TRAFFIC_RISK_CATALOG


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
    assert risk.risk_code == "TWR_NORMAL"

    decision = RiskDecisionAgent(TRAFFIC_RISK_CATALOG).decide(risk, t_sec=0.0)
    assert decision.level.value == "관심"
    assert decision.alert is False


def test_heavy_rain_and_blocked_traffic_escalates_to_gridlock():
    """★ 2026-08-21: 옛 코드명 WIR_ROAD_IMPASSABLE(침수·교통 혼합) →
    TWR_SEVERE_GRIDLOCK(순수 교통). 하천수위 없이 순수 교통·기상 신호만으로도
    같은 조건에서 여전히 「심각」까지 escalate 하는지 확인한다."""
    agent = SemanticAgent()
    risk = agent.infer(
        _weather(35.0, WeatherIntensity.very_heavy),
        _traffic(TrafficState.blocked, speed_drop=0.6, stalled=3),
    )
    assert risk.risk_code == "TWR_SEVERE_GRIDLOCK"

    decision = RiskDecisionAgent(TRAFFIC_RISK_CATALOG).decide(risk, t_sec=10.0)
    assert decision.level.value == "심각"
    assert decision.alert is True


def test_congestion_without_rain_is_not_weather_attributed():
    agent = SemanticAgent()
    risk = agent.infer(
        _weather(0.0, WeatherIntensity.none),
        _traffic(TrafficState.congested, speed_drop=0.6, stalled=1),
    )
    assert risk.risk_code == "TWR_NORMAL"
    assert "기상영향 아님" in risk.context


# ── 판정 임계값 config화 (2026-08-24, 왼쪽 메뉴 "교통위험 › 위험도
# 임계값" 화면 신설) ──────────────────────────────────────────────────────
# 강수·속도저하·정지차량 임계값이 코드 리터럴에서 SemanticAgent(cfg) 로
# 옮겨졌다. 지켜야 할 것.
#   ① cfg=None(기본값)이면 옮기기 전과 수치가 한 치도 다르지 않아야 한다
#      — 실사용 중인 판정이 리팩터 하나로 조용히 바뀌면 안 된다.
#   ② cfg 로 넘긴 값이 실제로 분기·점수 계산에 반영돼야 한다.

def test_기본_cfg는_리팩터_전과_같은_점수를_낸다():
    """옛 리터럴(0.45/0.35/0.20 가중치, 30.0·5.0 상한)을 그대로 옮겼는지
    수치로 고정해 둔다."""
    agent = SemanticAgent()  # cfg 없음 -> TRAFFIC_SEMANTIC_DEFAULTS
    risk = agent.infer(
        _weather(35.0, WeatherIntensity.very_heavy),
        _traffic(TrafficState.blocked, speed_drop=0.6, stalled=3),
    )
    assert risk.risk_code == "TWR_SEVERE_GRIDLOCK"
    # 0.45*min(35/30,1) + 0.35*0.6 + 0.20*min(3/5,1) = 0.45+0.21+0.12 = 0.78
    assert risk.score == 0.78


def test_cfg로_임계값을_올리면_판정이_바뀐다():
    """설정 화면에서 stalled_severe 를 올리면, 정지차량이 이전 기본값
    (2대)은 넘지만 새 값(5대)에는 못 미치는 상황에서 더 이상 심각으로
    격상되지 않아야 한다 — cfg 가 실제로 판정에 반영되는지 확인."""
    agent = SemanticAgent({"stalled_severe": 5})
    risk = agent.infer(
        _weather(35.0, WeatherIntensity.very_heavy),
        _traffic(TrafficState.blocked, speed_drop=0.6, stalled=3),
    )
    assert risk.risk_code != "TWR_SEVERE_GRIDLOCK"


def test_cfg의_일부_값만_바꿔도_나머지는_기본값을_쓴다():
    """전체를 다시 적을 필요 없이 바꾸고 싶은 값만 넘기면 된다(부분 병합)."""
    agent = SemanticAgent({"speed_drop_notable": 0.5})
    risk = agent.infer(
        _weather(0.0, WeatherIntensity.none),
        _traffic(TrafficState.free, speed_drop=0.3),  # 옛 기본값(0.2)은 넘지만 새 값(0.5) 미만
    )
    assert "감소" not in " ".join(risk.drivers)
