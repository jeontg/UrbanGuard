"""Semantic reasoning — rule/ontology fusion of weather x traffic.

Ported from flood3's ``agents/semantic_agent.py``. The key idea is *semantic
fusion*: identical congestion is classified as weather-driven risk if it's
raining, and as ordinary congestion otherwise.

PoC infers via ``knowledge/ontology.py``'s catalog + the rules below. A future
graph-DB backend can replace this while keeping ``infer()``'s signature.

★ 2026-08-21 flood/traffic 도메인 분리: 예전에는 이 클래스가 하천수위
(``river``, 명백한 직접 침수 신호)까지 받아 침수 판정과 융합했다. 이제
**순수 교통·기상 판정만** 한다 — 하천수위는 ``flood/river_risk.py``의
``FloodRiverAgent``가 완전히 독립적으로 처리한다(docs/202608210801 참고).

★ 2026-08-24: 판정 임계값(속도저하·정지차량·점수 가중치)을 리터럴에서
``cfg`` 딕셔너리로 뺐다 — 침수의 ``risk_config.yaml``(S-82)과 대칭이 되도록
``configs/traffic_risk_config.yaml``에서 편집할 수 있게 하기 위함(왼쪽 메뉴
"교통위험" 그룹의 "위험도 임계값" 화면). ``cfg=None``이면 이 파일에 옮기기
전과 **완전히 같은 값**(``TRAFFIC_SEMANTIC_DEFAULTS``)을 쓴다 — 기존 호출부(``run_poc.py``,
``runner.py``, 테스트)는 인자 없이 그대로 호출해도 동작이 바뀌지 않는다.
날씨 강도 구간(약함/보통/강함/매우강함)의 경계 자체와 그것들을 조합하는
판정 순서는 그대로 코드에 남겨 뒀다 — 범주 조합 로직이라 단순 편집 화면에
넣으면 오히려 판정을 깨뜨릴 위험이 크다(침수 설정의 weights/grade_bins가
읽기전용인 것과 같은 이유).
"""
from __future__ import annotations

from ...models import (
    LEVEL_SEVERITY,
    TrafficMetrics,
    TrafficState,
    WeatherImpactRisk,
    WeatherIntensity,
    WeatherState,
)
from ..knowledge.ontology import TRAFFIC_RISK_CATALOG

_HEAVY = (WeatherIntensity.heavy, WeatherIntensity.very_heavy)
_RAINY_MID = (WeatherIntensity.moderate, WeatherIntensity.heavy, WeatherIntensity.very_heavy)

# configs/traffic_risk_config.yaml 이 없거나 값이 빠졌을 때 쓰는 기본값 —
# 2026-08-24 이전까지 이 파일에 리터럴로 박혀 있던 것과 동일한 값이다.
# runner.py가 이 값을 그대로 가져다 YAML 병합의 기본값으로 쓴다(공개 이름).
TRAFFIC_SEMANTIC_DEFAULTS = {
    "speed_drop_notable": 0.2, "speed_drop_heavy": 0.4,
    "queue_len_notable": 3, "stalled_notable": 1, "stalled_severe": 2,
    "score_weight_rain": 0.45, "score_weight_speed_drop": 0.35,
    "score_weight_stalled": 0.20, "rain_cap_mm_h": 30.0, "stalled_cap": 5.0,
}


class SemanticAgent:
    """Rule/ontology-based fusion inference. ``infer()`` stays stable even if
    the backend is later replaced by a graph DB."""

    def __init__(self, cfg: dict | None = None) -> None:
        c = {**TRAFFIC_SEMANTIC_DEFAULTS, **(cfg or {})}
        self.speed_drop_notable = float(c["speed_drop_notable"])
        self.speed_drop_heavy = float(c["speed_drop_heavy"])
        self.queue_len_notable = float(c["queue_len_notable"])
        self.stalled_notable = float(c["stalled_notable"])
        self.stalled_severe = float(c["stalled_severe"])
        self.score_weight_rain = float(c["score_weight_rain"])
        self.score_weight_speed_drop = float(c["score_weight_speed_drop"])
        self.score_weight_stalled = float(c["score_weight_stalled"])
        self.rain_cap_mm_h = float(c["rain_cap_mm_h"])
        self.stalled_cap = float(c["stalled_cap"])

    def infer(self, weather: WeatherState, traffic: TrafficMetrics,
              situation: dict | None = None, context: dict | None = None) -> WeatherImpactRisk:
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
        if drop > self.speed_drop_notable:
            drivers.append(f"평균속도 {drop * 100:.0f}% 감소")
        if queue >= self.queue_len_notable:
            drivers.append(f"정체 대기열 {queue}대")
        if stalled >= self.stalled_notable:
            drivers.append(f"정지차량 {stalled}대")

        # ── traffic x weather fusion (순수 교통·기상, 하천수위 없음) ──
        if (raining and inten in _HEAVY and traffic.state == TrafficState.blocked
                and stalled >= self.stalled_severe):
            code = "TWR_SEVERE_GRIDLOCK"
        elif raining and inten in _RAINY_MID and stalled >= self.stalled_severe:
            code = "TWR_SEVERE_CONGESTION"
        elif raining and drop > self.speed_drop_heavy and traffic.state == TrafficState.congested:
            code = "TWR_RAIN_CONGESTION"
        elif raining and drop > self.speed_drop_notable:
            code = "TWR_RAIN_CONGESTION" if traffic.state == TrafficState.congested else "TWR_RAIN_ONSET"
        elif raining:
            code = "TWR_RAIN_ONSET"
        else:
            code = "TWR_NORMAL"

        # ── semantic context sentence ──
        if raining and drop > self.speed_drop_notable:
            ctx = f"{block}: 강우와 교통 정체가 동시 관측됨 — 강수 영향으로 추정"
        elif drop > self.speed_drop_notable:
            ctx = f"{block}: 강수 없음 — 기상영향 아님(일반 정체 가능성)"
        elif raining:
            ctx = f"{block}: 강우 관측, 교통 영향은 경미"
        else:
            ctx = f"{block}: 특이사항 없음"

        rdef = TRAFFIC_RISK_CATALOG[code]
        # ★ 하천 항(0.20 river_ratio)을 뺀 나머지 3항의 상대 비율을 그대로
        #   유지해 재정규화했다(0.35/0.8, 0.30/0.8, 0.15/0.8 → 0.4375/0.375/0.1875,
        #   반올림 0.45/0.35/0.20). river 제거 전/후 score의 절대 의미가 달라지는
        #   것을 피하려는 것이 아니라, "이 셋만으로도 합이 1에 가깝게" 만드는 것.
        score = min(1.0, self.score_weight_rain * min(rain / self.rain_cap_mm_h, 1.0)
                    + self.score_weight_speed_drop * drop
                    + self.score_weight_stalled * min(stalled / self.stalled_cap, 1.0))
        return WeatherImpactRisk(
            risk_code=code, risk_name=rdef.name,
            drivers=drivers or ["정상"], context=ctx, score=round(score, 3))
