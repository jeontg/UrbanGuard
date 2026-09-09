"""FloodRiverAgent — 하천수위 전용 판정, 교통 신호는 절대 참조하지 않는다
(S-88 이후, flood/traffic 도메인 분리, 2026-08-21).

## 왜 이 시험이 있나

예전에는 ``traffic_weather.agents.semantic_agent.SemanticAgent``가 하천수위
(river)를 받아 교통 신호와 융합해 판정했다(``tests/traffic_weather/
test_semantic_and_decision.py``의 옛 ``test_river_danger_escalates_even_
without_much_traffic_impact``). 이제 하천수위는 ``FloodRiverAgent``가
**완전히 독립적으로**(교통 신호 없이) 판정한다 — 이 시험이 그 이전분이다.
"""
from tot_dashboard.flood.river_risk import FloodRiverAgent
from tot_dashboard.models import RiverState, RiverStatus


def test_하천수위_위험이면_교통_신호_없이도_경계로_올라간다():
    """★ 이전됨 — 예전 시험(교통 신호가 거의 없어도 하천 위험만으로
    escalate)의 취지를 그대로 유지한다. 다만 이제 traffic 파라미터 자체가
    없다 — FloodRiverAgent는 애초에 받을 수 없는 구조다."""
    agent = FloodRiverAgent()
    river = RiverState(level_m=4.5, status=RiverStatus.danger, ratio=1.1, station="test")
    risk = agent.infer(river, raining=False)
    assert risk.risk_code == "FR_RIVER_ALERT"  # 비가 안 오면 danger도 alert로


def test_비_동반_하천위험은_최고등급까지_올라간다():
    agent = FloodRiverAgent()
    river = RiverState(level_m=4.5, status=RiverStatus.danger, ratio=1.1, station="test")
    risk = agent.infer(river, raining=True)
    assert risk.risk_code == "FR_RIVER_DANGER"


def test_하천이_없으면_정상이다():
    agent = FloodRiverAgent()
    risk = agent.infer(None, raining=True)
    assert risk.risk_code == "FR_NORMAL"


def test_주의_수위는_비_없으면_정상으로_본다():
    """★ 하천수위 단독으로는 아직 「직접 침수 신호」로 볼 수 없는 낮은
    단계 — 강우가 동반돼야 주의로 올린다(옛 SemanticAgent 규칙과 동일 취지)."""
    agent = FloodRiverAgent()
    river = RiverState(level_m=2.6, status=RiverStatus.advisory, ratio=0.65, station="test")
    assert agent.infer(river, raining=False).risk_code == "FR_NORMAL"
    assert agent.infer(river, raining=True).risk_code == "FR_RIVER_ADVISORY"


def test_경계_수위는_강우_여부와_무관하게_경계다():
    agent = FloodRiverAgent()
    river = RiverState(level_m=3.3, status=RiverStatus.alert, ratio=0.825, station="test")
    assert agent.infer(river, raining=False).risk_code == "FR_RIVER_ALERT"
    assert agent.infer(river, raining=True).risk_code == "FR_RIVER_ALERT"


def test_교통_신호를_받는_파라미터가_아예_없다():
    """★ 구조적 보장 — FloodRiverAgent.infer()는 traffic/vehicles 파라미터를
    받지 않는다. 하천수위가 교통 신호와 뒤섞일 수 없다는 것을 코드로 확인."""
    import inspect
    sig = inspect.signature(FloodRiverAgent.infer)
    assert set(sig.parameters) == {"self", "river", "raining", "context"}
