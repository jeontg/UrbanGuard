"""하천수위 전용 판정 카탈로그 — 순수 침수 신호만 (2026-08-21 신설).

flood/traffic 도메인 분리로 옛 ``traffic_weather``의 ``RISK_CATALOG``
(WIR_*, 강우·정체·하천수위 혼합)에서 하천수위 부분만 떼어냈다. 여기 있는
4개 코드(``FR_*``)는 **하천수위만 보고** 판정하며, 교통 신호(정체·정지차량)
는 전혀 참조하지 않는다 — 그건 ``traffic/`` 쪽 ``TRAFFIC_RISK_CATALOG``
(``TWR_*``)의 몫이다.

``RiskDef``를 ``traffic_weather.knowledge.ontology``에서 다시 import하지
않고 여기서 독립적으로 정의한 이유: flood와 traffic이 서로의 모듈을
참조하지 않아야 "완전히 독립된 도메인"이라는 이번 분리의 목적에 맞는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import RiskLevel


@dataclass(frozen=True)
class RiskDef:
    code: str
    name: str
    level: RiskLevel
    recommendation: str


FLOOD_RIVER_CATALOG: dict[str, RiskDef] = {
    "FR_NORMAL": RiskDef(
        "FR_NORMAL", "정상", RiskLevel.interest,
        "정상 모니터링 유지"),
    "FR_RIVER_ADVISORY": RiskDef(
        "FR_RIVER_ADVISORY", "하천수위 주의(강우 동반)", RiskLevel.caution,
        "모니터링 강화"),
    # 구 WIR_FLOOD_RISK의 하천측 절반.
    "FR_RIVER_ALERT": RiskDef(
        "FR_RIVER_ALERT", "하천수위 경계·침수우려", RiskLevel.alert,
        "순찰 강화·핫스팟 공유 / 배수시설 점검"),
    # 구 WIR_ROAD_IMPASSABLE의 하천측 절반.
    "FR_RIVER_DANGER": RiskDef(
        "FR_RIVER_DANGER", "하천수위 위험·도로잠김우려", RiskLevel.serious,
        "[긴급] 즉시 출동·우회 안내·통제센터 보고"),
}
