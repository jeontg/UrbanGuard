"""Flood risk **measurement** (RiskEngine) + **prediction** (RiskPredictor).

Unified from underpath_flood_dashboard's ``src/risk_engine.py`` and flood3's
``flood/risk_engine.py`` — the integration review found these to be near
line-for-line identical (docs/integration_plan.md section 2), differing only
in: (a) the constructor's alert-config fallback (flood3 falls back to the
risk config itself when no separate alert config is given — kept here as the
more defensive behavior), and (b) the ``traffic_state`` comparison values,
because the two originals received differently-typed metrics
(underpath_flood_dashboard: plain strings; flood3: the ``TrafficState`` enum
from ``models.py``). This module operates on the single canonical
``flood.metrics_core.FloodMetrics`` dataclass, which standardizes on
``TrafficState`` (see docs/integration_plan.md section 4, decision #4).
"""
from __future__ import annotations

import dataclasses
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .metrics_core import FloodMetrics

GRADE_KR = {1: "매우 낮음", 2: "낮음", 3: "보통", 4: "높음", 5: "매우 높음"}
TREND_KR = {
    "surge": "급상승", "rising": "상승", "stable": "유지",
    "falling": "하강", "insufficient": "정보 부족",
}
COMPONENT_KR = {
    "area": "도로 침수 면적",
    "low_point": "저지대 침수",
    "lane": "침수 경계선 침범",
    "expansion": "확산 속도",
    "tire": "차량 바퀴 침수",
    "person": "보행자 위험",
}
THRESHOLD_KR = {"caution": "주의", "danger": "위험", "shutdown": "통제"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class RiskResult:
    risk_score: float
    risk_grade: int
    grade_label: str
    components: dict[str, float]
    raw_components: dict[str, float]
    top_reason: str
    floored_by: str | None = None


@dataclass
class PredictionResult:
    trend_slope: float
    trend_label: str
    confidence: float
    pred_ratio: dict[int, float] = field(default_factory=dict)
    pred_risk: dict[int, float] = field(default_factory=dict)
    eta: dict[str, float | None] = field(default_factory=dict)
    curve: list[tuple[float, float]] = field(default_factory=list)


class RiskEngine:
    def __init__(self, risk_config: dict[str, Any] | None = None,
                 alert_config: dict[str, Any] | None = None) -> None:
        rc = risk_config or {}
        ac = alert_config or rc
        # ⚠️ 2026-08-21 flood/traffic 도메인 분리 — "traffic"(정체·정지 차량,
        # 6%) 가중치를 제거했다. 나머지 6개 요소의 비율은 그대로 두고
        # (0.28/0.24/0.10/0.10/0.12/0.10 그대로), score()의 wsum 정규화가
        # 이 6개만으로 자동 재정규화한다 — 상대 비율이 보존된다.
        self.weights = dict(rc.get("weights", {
            "area": 0.28, "low_point": 0.24, "lane": 0.10, "expansion": 0.10,
            "tire": 0.12, "person": 0.10,
        }))
        self.expansion_ref = float(rc.get("expansion_ref", 0.05))
        self.person_floor = float(rc.get("person_danger_floor", 85.0))
        self.shutdown_rising_floor = float(rc.get("shutdown_rising_floor", 90.0))
        self.grade_bins = list(rc.get("grade_bins", [20, 40, 60, 80]))
        # thresholds reused from the alert config so risk & alerts stay aligned
        self.ratio_watch = float(ac.get("ratio_watch", 0.02))
        self.ratio_caution = float(ac.get("ratio_caution", 0.08))
        self.ratio_danger = float(ac.get("ratio_danger", 0.20))
        self.ratio_shutdown = float(ac.get("ratio_shutdown", 0.35))

    def _raw_components(self, m: FloodMetrics) -> dict[str, float]:
        """★ 2026-08-21: 순수 침수 6요소만 계산한다 — "traffic"(정체·정지
        차량) 성분을 제거했다. `FloodMetrics`에서 `traffic_state`/
        `stopped_vehicles_near_water` 필드 자체가 없어졌으므로 여기서
        참조할 수도 없다(교통 도메인 분리, docs/202608210801 참고)."""
        ratio = m.water_area_ratio
        area = ratio / self.ratio_shutdown if self.ratio_shutdown > 0 else ratio
        tire = min(1.0, m.max_vehicle_submersion * 1.5)
        if m.vehicles_tire_in_water > 0:
            tire = max(tire, 0.4)
        return {
            "area": _clamp(area),
            "low_point": _clamp(m.low_point_water_ratio),
            "lane": 1.0 if m.water_crosses_lane else 0.0,
            "expansion": _clamp(m.water_expansion_rate / self.expansion_ref)
            if self.expansion_ref > 0 else 0.0,
            "tire": _clamp(tire),
            "person": 1.0 if m.persons_in_danger > 0 else 0.0,
        }

    def grade_of(self, score: float) -> int:
        b = self.grade_bins
        for i, edge in enumerate(b):
            if score < edge:
                return i + 1
        return len(b) + 1

    def score(self, m: FloodMetrics) -> RiskResult:
        raw = self._raw_components(m)
        wsum = sum(self.weights.get(k, 0.0) for k in raw) or 1.0
        contrib = {k: self.weights.get(k, 0.0) * raw[k] / wsum * 100.0 for k in raw}
        score = float(sum(contrib.values()))

        floored_by = None
        if m.persons_in_danger > 0 and score < self.person_floor:
            score, floored_by = self.person_floor, "person"
        if (m.water_area_ratio >= self.ratio_shutdown and m.water_expansion_rate > 0
                and score < self.shutdown_rising_floor):
            score, floored_by = self.shutdown_rising_floor, "shutdown_rising"

        score = round(_clamp(score, 0.0, 100.0), 1)
        grade = self.grade_of(score)
        top_key = max(contrib, key=contrib.get) if any(contrib.values()) else "area"
        top_reason = COMPONENT_KR.get(top_key, top_key)
        return RiskResult(
            risk_score=score, risk_grade=grade, grade_label=GRADE_KR.get(grade, "?"),
            components=contrib, raw_components=raw, top_reason=top_reason,
            floored_by=floored_by,
        )

    def score_for_ratio(self, m: FloodMetrics, projected_ratio: float) -> float:
        """Risk score if only the water ratio changed to ``projected_ratio``
        (used by the predictor to forecast future risk)."""
        cur = max(m.water_area_ratio, 1e-6)
        growth = _clamp(projected_ratio / cur, 0.0, 4.0)
        projected = dataclasses.replace(
            m,
            water_area_ratio=_clamp(projected_ratio),
            low_point_water_ratio=_clamp(m.low_point_water_ratio * growth),
        )
        return self.score(projected).risk_score


class RiskPredictor:
    def __init__(self, engine: RiskEngine, risk_config: dict[str, Any] | None = None) -> None:
        rc = risk_config or {}
        self.engine = engine
        self.horizons = [int(h) for h in rc.get("horizons_sec", [5, 10])]
        self.window = float(rc.get("predict_window_sec", 12.0))
        self.min_points = int(rc.get("predict_min_points", 3))
        self.trend_surge = float(rc.get("trend_surge", 0.03))
        self.trend_rising = float(rc.get("trend_rising", 0.005))
        self.trend_falling = float(rc.get("trend_falling", -0.005))
        self.eta_max = float(rc.get("eta_max_sec", 600))
        self._hist: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self._hist.clear()

    def _trend_label(self, slope: float) -> str:
        if slope >= self.trend_surge:
            return "surge"
        if slope >= self.trend_rising:
            return "rising"
        if slope <= self.trend_falling:
            return "falling"
        return "stable"

    def _eta(self, cur_ratio: float, slope: float, thr: float) -> float | None:
        if cur_ratio >= thr:
            return 0.0
        if slope <= 1e-5:
            return None
        eta = (thr - cur_ratio) / slope
        return eta if 0 <= eta <= self.eta_max else None

    def update(self, m: FloodMetrics) -> PredictionResult:
        t, ratio = m.timestamp_sec, m.water_area_ratio
        self._hist.append((t, ratio))
        while self._hist and t - self._hist[0][0] > self.window and len(self._hist) > 2:
            self._hist.popleft()

        max_h = max(self.horizons) if self.horizons else 10
        if len(self._hist) < self.min_points:
            return PredictionResult(
                trend_slope=0.0, trend_label="insufficient", confidence=0.0,
                pred_ratio={h: ratio for h in self.horizons},
                pred_risk={h: m.risk_score for h in self.horizons},
                eta={k: None for k in ("caution", "danger", "shutdown")},
                curve=[(0.0, ratio)],
            )

        ts = np.array([p[0] for p in self._hist], dtype=float)
        rs = np.array([p[1] for p in self._hist], dtype=float)
        slope, intercept = np.polyfit(ts - ts[0], rs, 1)
        pred_line = slope * (ts - ts[0]) + intercept
        ss_res = float(np.sum((rs - pred_line) ** 2))
        ss_tot = float(np.sum((rs - rs.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else (1.0 if ss_res < 1e-12 else 0.0)
        confidence = round(_clamp(r2) * _clamp(len(self._hist) / (self.min_points + 2)), 3)

        pred_ratio = {h: _clamp(ratio + slope * h) for h in self.horizons}
        pred_risk = {h: self.engine.score_for_ratio(m, pred_ratio[h]) for h in self.horizons}
        eta = {
            "caution": self._eta(ratio, slope, self.engine.ratio_caution),
            "danger": self._eta(ratio, slope, self.engine.ratio_danger),
            "shutdown": self._eta(ratio, slope, self.engine.ratio_shutdown),
        }
        curve = [(float(dt), _clamp(ratio + slope * dt)) for dt in range(0, max_h + 1)]
        return PredictionResult(
            trend_slope=float(slope), trend_label=self._trend_label(slope),
            confidence=confidence, pred_ratio=pred_ratio, pred_risk=pred_risk,
            eta=eta, curve=curve,
        )


def write_back(m: FloodMetrics, risk: RiskResult, pred: PredictionResult,
               horizon: int = 10) -> None:
    """Copy the compact risk/prediction fields onto FloodMetrics (for CSV/UI)."""
    m.risk_score = risk.risk_score
    m.risk_grade = risk.risk_grade
    m.risk_trend = pred.trend_label
    m.pred_water_ratio_10s = round(pred.pred_ratio.get(horizon, m.water_area_ratio), 4)
    m.pred_risk_10s = pred.pred_risk.get(horizon, m.risk_score)
    eta_d = pred.eta.get("danger")
    m.eta_danger_sec = -1.0 if eta_d is None else round(eta_d, 1)
