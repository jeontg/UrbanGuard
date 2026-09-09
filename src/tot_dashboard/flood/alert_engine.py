"""Rule-based flood alert engine, levels 1..5 (never 0).

    1 Normal                      green
    2 Watch                       blue / light yellow
    3 Caution                     yellow
    4 Danger                      orange
    5 Road Shutdown Recommended   red

Key rule: high alerts must NOT fire from a single frame. A raised condition
has to persist for ``persistence_frames`` processed frames (~3 s at 1 FPS)
before the confirmed level escalates, and it de-escalates only after
``cooldown_frames`` calmer frames. Levels 1-2 react immediately (low severity).

Ported from underpath_flood_dashboard's ``src/alert_engine.py``. This feature
has no equivalent in flood3 (flood3 only has the continuous RiskEngine score +
the official 4-level MOIS grade, no discrete escalating alert level) — see
docs/integration_plan.md section 3-4.

★ TRAFFIC-STATE MAPPING — FLAGGED FOR REVIEW (see docs/integration_plan.md
section 9, decision #6): the original alert_engine.py compared against
underpath_flood_dashboard's own 6-value traffic_state vocabulary
(empty/unknown/normal/slow/stopped/jammed, computed by its own
self-contained MetricsEngine). The canonical ``flood.metrics_core.FloodMetrics``
now standardizes on flood3's 4-value ``TrafficState`` enum instead (see
docs/integration_plan.md section 4, decision #4). The mapping below is this
port's judgment call and has NOT been validated against real incident data:

    empty / unknown / normal  -> TrafficState.free       (no congestion signal)
    slow                      -> TrafficState.slow
    stopped (few vehicles)    -> TrafficState.congested
    jammed  (>= N vehicles)   -> TrafficState.blocked

A domain expert should confirm this before the alert thresholds are relied on
for real shutdown recommendations.

★ 2026-08-21 flood/traffic 도메인 분리: 이 엔진은 S-23 오프라인 파이프라인
(``standalone_pipeline.py``) 전용으로만 남았고, 그 경로가 여전히
``traffic_state``/``stopped_vehicles_near_water``를 함께 쓰므로 이 파일은
``StandaloneFloodMetrics``를 그대로 받는다 — 실시간 경로(RiskEngine)는 이미
이 두 필드 없이 순수화됐지만, 이 오프라인 규칙엔진 자체의 분리는 이번 범위에서
제외했다(작업량 대비 가치가 낮다는 판단, docs/202608210801 5절 참고).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from ..models import TrafficState
from .metrics_core import StandaloneFloodMetrics as FloodMetrics

LEVEL_NAME = {
    1: "정상",
    2: "관찰",
    3: "주의",
    4: "위험",
    5: "도로 통제 권고",
}

LEVEL_COLOR = {  # (color name, hex) for the dashboard status chip
    1: ("Green", "#2E7D32"),
    2: ("Blue", "#1565C0"),
    3: ("Yellow", "#F9A825"),
    4: ("Orange", "#EF6C00"),
    5: ("Red", "#C62828"),
}

# ASCII names for OpenCV overlays (cv2.putText cannot render Korean glyphs).
LEVEL_NAME_EN = {
    1: "Normal", 2: "Watch", 3: "Caution", 4: "Danger", 5: "Shutdown Advised",
}


@dataclass
class _Rule:
    level: int
    text: str
    active: bool


class AlertEngine:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        c = config or {}
        self.ratio_watch = float(c.get("ratio_watch", 0.02))
        self.ratio_caution = float(c.get("ratio_caution", 0.08))
        self.ratio_danger = float(c.get("ratio_danger", 0.20))
        self.ratio_shutdown = float(c.get("ratio_shutdown", 0.35))
        self.expansion_rate_rising = float(c.get("expansion_rate_rising", 0.01))
        self.persistence_frames = int(c.get("persistence_frames", 3))
        self.cooldown_frames = int(c.get("cooldown_frames", 3))

        self.confirmed = 1
        maxlen = max(self.persistence_frames, self.cooldown_frames, 1)
        self._raw_levels: deque[int] = deque(maxlen=maxlen)
        self._last_reasons: list[str] = []

    def _evaluate_rules(self, m: FloodMetrics) -> list[_Rule]:
        ratio = m.water_area_ratio
        rising = m.water_expansion_rate >= self.expansion_rate_rising
        stopped = m.traffic_state in (TrafficState.congested, TrafficState.blocked)

        traffic_kr = m.traffic_state.value
        rules = [
            # --- Level 5: shutdown recommended ---
            _Rule(5, f"도로 ROI 내 심각한 침수 (비율 {ratio:.2f})",
                  ratio >= self.ratio_shutdown),
            _Rule(5, "저지대 ROI에 물 존재, 계속 확산 중",
                  m.water_near_low_point and rising),
            _Rule(5, f"침수·저지대 구역 내 보행자 {m.persons_in_danger}명",
                  m.persons_in_danger > 0),
            _Rule(5, f"물 속·근처 정지 차량 {m.stopped_vehicles_near_water}대",
                  m.stopped_vehicles_near_water > 0 and ratio >= self.ratio_caution),

            # --- Level 4: danger ---
            _Rule(4, f"도로 ROI 내 높은 침수 (비율 {ratio:.2f})",
                  ratio >= self.ratio_danger),
            _Rule(4, "도로 ROI 전반으로 물 확산 중",
                  ratio >= self.ratio_caution and rising),
            _Rule(4, "물이 침수 경계선을 넘음",
                  m.water_crosses_lane),
            _Rule(4, f"물에 닿은 차량 {m.vehicles_touching_water}대",
                  m.vehicles_touching_water > 0),
            _Rule(4, f"물 근처 교통 상태: {traffic_kr}",
                  stopped and ratio >= self.ratio_watch),

            # --- Level 3: caution ---
            _Rule(3, f"도로 ROI 내 물이 주의 기준 초과 (비율 {ratio:.2f})",
                  ratio >= self.ratio_caution),
            _Rule(3, "지하차도 저지대 부근에 물 존재",
                  m.water_near_low_point),
            _Rule(3, "물 근처 차량 서행",
                  m.traffic_state == TrafficState.slow and ratio >= self.ratio_watch),

            # --- Level 2: watch ---
            _Rule(2, f"도로 ROI 내 소량의 물 감지 (비율 {ratio:.2f})",
                  ratio >= self.ratio_watch),
            _Rule(2, "도로 ROI 내부에 미량의 물 감지",
                  m.water_area_pixels > 0),
        ]
        return rules

    def _raw_level(self, m: FloodMetrics) -> tuple[int, list[str]]:
        rules = self._evaluate_rules(m)
        active = [r for r in rules if r.active]
        if not active:
            return 1, ["유의미한 침수 없음, 교통 정상"]
        level = max(r.level for r in active)
        reasons = [r.text for r in active if r.level == level]
        return level, reasons

    def update(self, m: FloodMetrics) -> FloodMetrics:
        raw, reasons = self._raw_level(m)
        self._raw_levels.append(raw)
        self._last_reasons = reasons
        levels = list(self._raw_levels)

        # --- escalation: a higher level must persist for persistence_frames ---
        last_p = levels[-self.persistence_frames:]
        if len(last_p) >= self.persistence_frames:
            sustained = min(last_p)
        else:
            # during warm-up, do not allow escalation above Watch (level 2)
            sustained = min(min(last_p), 2)

        if sustained > self.confirmed:
            self.confirmed = sustained
        elif sustained < self.confirmed:
            # --- de-escalation: only after cooldown_frames calmer readings ---
            last_c = levels[-self.cooldown_frames:]
            relaxed = max(last_c)
            if relaxed < self.confirmed:
                self.confirmed = max(relaxed, sustained)

        m.alert_level = self.confirmed
        m.alert_reason = self._reason_for_confirmed(raw, reasons)
        return m

    def _reason_for_confirmed(self, raw: int, reasons: list[str]) -> str:
        if raw >= self.confirmed and reasons:
            return "; ".join(reasons)
        if self.confirmed == 1:
            return "유의미한 침수 없음, 교통 정상"
        return f"레벨 {self.confirmed}({LEVEL_NAME[self.confirmed]}) 유지 중, 완화 대기"

    @staticmethod
    def describe(level: int) -> tuple[str, str, str]:
        """Return (status_name, color_name, hex) for a level."""
        name = LEVEL_NAME.get(level, "Unknown")
        color_name, hex_ = LEVEL_COLOR.get(level, ("Gray", "#616161"))
        return name, color_name, hex_

    @staticmethod
    def describe_en(level: int) -> tuple[str, str]:
        """(ascii_name, hex) for OpenCV overlays that can't render Korean."""
        _, hex_ = LEVEL_COLOR.get(level, ("Gray", "#616161"))
        return LEVEL_NAME_EN.get(level, "Unknown"), hex_
