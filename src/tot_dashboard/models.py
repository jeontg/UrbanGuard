"""Shared domain models (Pydantic v2).

Ported from flood3's ``models.py`` — the data contract between the perception
(rainfall x traffic) pipeline and the semantic-fusion agents. flood's risk
engine and alert engine (see flood/) also depend on ``TrafficState``, which is
why this module lives at the package root rather than under a single domain
subpackage: both flood/ and traffic_weather/ import from it.

Risk grading uses Korea's 4-level MOIS crisis-alert scale (관심/주의/경계/심각).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WeatherIntensity(str, Enum):
    none = "없음"
    light = "약함"
    moderate = "보통"
    heavy = "강함"
    very_heavy = "매우강함"


class TrafficState(str, Enum):
    free = "원활"
    slow = "서행"
    congested = "정체"
    blocked = "정지"


class RiskLevel(str, Enum):
    """행정안전부 위기경보 4단계."""

    interest = "관심"  # Blue
    caution = "주의"  # Yellow
    alert = "경계"  # Orange
    serious = "심각"  # Red


LEVEL_SEVERITY: dict[RiskLevel, int] = {
    RiskLevel.interest: 0,
    RiskLevel.caution: 1,
    RiskLevel.alert: 2,
    RiskLevel.serious: 3,
}


class WeatherState(BaseModel):
    t_sec: float
    rain_mm_h: float = Field(ge=0, description="시간당 강수량(mm/h)")
    intensity: WeatherIntensity
    trend: str = "유지"
    delta_mm_h: float = 0.0
    source: str = "mock"


class Detection(BaseModel):
    """단일 차량 검출(YOLO 또는 합성). traffic_weather 도메인 전용 — flood 도메인은
    별도의 dataclass 기반 Detection(flood/object_detection.py)을 쓴다(통합 계획
    검토 결과 두 도메인의 탐지 파이프라인 아키텍처가 서로 달라 공용화하지 않기로
    결정함, docs/integration_plan.md 참조)."""

    bbox: tuple[float, float, float, float]
    conf: float = 1.0
    cls: str = "car"
    track_id: int | None = None


class TrafficMetrics(BaseModel):
    t_sec: float
    n_vehicles: int
    mean_speed: float = Field(description="평균 이동속도(px/s)")
    mean_speed_kmh: float | None = Field(
        default=None,
        description="평균 실측 속도(km/h). 지면 캘리브레이션 없으면 None"
                     "(2026-08-26 — 예전 고정계수 mpp=0.06 가짜값을 대체)")
    speed_drop: float = Field(ge=0, le=1, description="속도급감비 0~1 (1 - 현재/자유흐름)")
    density: float = Field(ge=0, le=1, description="화면 점유 밀도 0~1")
    queue_len: int = Field(ge=0, description="정체 대기열 추정(연속 저속 차량 수)")
    stalled: int = Field(ge=0, description="정지차량 수")
    state: TrafficState


class VehicleObject(BaseModel):
    track_id: int
    cls: str = "car"
    bbox: tuple[float, float, float, float]
    speed: float = Field(description="현재 속도(px/s)")
    speed_kmh: float | None = Field(
        default=None, description="현재 실측 속도(km/h). 캘리브레이션 없으면 None")
    speed_drop: float = Field(ge=0, le=1, description="자유흐름 대비 감속 0~1")
    age: int = Field(description="추적 지속 프레임 수")
    stalled: bool = False


class PerceptionState(BaseModel):
    t_sec: float
    block_id: str
    vehicles: list[VehicleObject]
    persons: list[tuple[float, float]] = []
    weather: WeatherState
    metrics: TrafficMetrics
    # 돌발상황(보행자·역주행·사고 의심) — hold 창 안의 활성 사건 목록.
    # dict 로 담는다(도메인 dataclass를 여기서 임포트하면 순환참조가 됨) —
    # 실제 형태는 traffic_weather.perception.incident_events.TrafficIncident
    # .to_dict() 가 정한다.
    incidents: list[dict] = []


class WeatherImpactRisk(BaseModel):
    risk_code: str
    risk_name: str
    drivers: list[str]
    context: str
    score: float = Field(ge=0, le=1)


class Decision(BaseModel):
    t_sec: float
    risk_code: str
    risk_name: str
    level: RiskLevel
    severity: int
    score: float
    recommendation: str
    drivers: list[str]
    context: str
    alert: bool


class RiverStatus(str, Enum):
    normal = "정상"
    advisory = "주의"
    alert = "경계"
    danger = "위험"


class RiverState(BaseModel):
    level_m: float = Field(description="현재 수위(m)")
    status: RiverStatus
    ratio: float = Field(ge=0, description="위험수위 대비 비율(level/danger)")
    station: str = ""
    source: str = "mock"
