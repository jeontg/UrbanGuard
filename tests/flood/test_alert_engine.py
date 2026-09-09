"""Verify alert_engine.py's rules fire correctly against every value of the
canonical TrafficState enum (this is the mapping introduced during the merge —
see alert_engine.py's module docstring and docs/integration_plan.md section
9 decision #6 — so it needs its own explicit coverage, not just inherited
trust from the original underpath_flood_dashboard tests which used plain
strings).
"""
from tot_dashboard.flood.alert_engine import AlertEngine
from tot_dashboard.flood.metrics_core import StandaloneFloodMetrics
from tot_dashboard.models import TrafficState


def _confirmed_level(traffic_state: TrafficState, water_area_ratio: float, persistence: int = 3) -> tuple[int, str]:
    """★ 2026-08-21: AlertEngine은 S-23 오프라인 파이프라인 전용으로만
    남았고, 그 경로가 여전히 traffic_state를 쓰므로 여기선
    StandaloneFloodMetrics를 그대로 쓴다(flood/traffic 도메인 분리 후에도
    이 시험의 목적 — traffic_state별 규칙 발동 검증 — 은 그대로 유효)."""
    engine = AlertEngine({"persistence_frames": persistence, "cooldown_frames": persistence})
    level = 1
    reason = ""
    for i in range(persistence):
        m = StandaloneFloodMetrics(
            frame_number=i, timestamp_sec=float(i),
            water_area_ratio=water_area_ratio, traffic_state=traffic_state,
        )
        engine.update(m)
        level, reason = m.alert_level, m.alert_reason
    return level, reason


def test_free_traffic_with_watch_level_water_does_not_escalate_on_traffic_alone():
    level, _ = _confirmed_level(TrafficState.free, water_area_ratio=0.03)
    assert level == 2  # Watch, from the ratio_watch rule alone


def test_slow_traffic_triggers_caution_rule():
    level, reason = _confirmed_level(TrafficState.slow, water_area_ratio=0.03)
    assert level == 3
    assert "서행" in reason


def test_congested_traffic_triggers_danger_rule():
    level, reason = _confirmed_level(TrafficState.congested, water_area_ratio=0.03)
    assert level == 4
    assert "정체" in reason


def test_blocked_traffic_triggers_danger_rule():
    level, reason = _confirmed_level(TrafficState.blocked, water_area_ratio=0.03)
    assert level == 4
    assert "정지" in reason


def test_no_water_stays_normal():
    level, _ = _confirmed_level(TrafficState.free, water_area_ratio=0.0)
    assert level == 1
