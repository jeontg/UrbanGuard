"""Incident briefing report generator (orchestration stage).

Ported from flood3's ``orchestrator/report_generator.py``. Numeric/
categorical fields (score, level, drivers, recommendation) stay
rule-computed **fixed fields**; only the "종합 판단" narrative paragraph is
LLM-generated (optional), falling back to a rule-derived sentence if no LLM
narrative was produced. This is a deliberate anti-hallucination design: the
LLM can only narrate/connect facts, never invent or alter them.
"""
from __future__ import annotations

import os
from datetime import datetime

from ..models import Decision, TrafficMetrics, WeatherState

AI_SRC = "AI 서술 · Gemini VLM"
RULE_SRC = "규칙 서술"


def _render(*, risk_name: str, level: str, severity: int, score: float,
            t_sec: float, location: str, risk_code: str,
            rain_mm_h: float, intensity: str, mean_speed: float, speed_drop: float,
            state: str, queue_len: int, stalled: int, drivers, recommendation: str,
            summary: str, narr_src: str, mean_speed_kmh=None) -> str:
    """Assembles the fixed fields + narrative (summary) into a briefing MD;
    only summary/narr_src differ by narrative source."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    drv = "\n".join(f"- {d}" for d in (drivers or ["정상"]))
    kmh = f" ({mean_speed_kmh}km/h)" if mean_speed_kmh is not None else ""
    return f"""# 사건 브리핑 — {risk_name} ([{level}])

| 항목 | 내용 |
|---|---|
| 생성시각 | {now} |
| 영상시각(t) | {t_sec:.1f}s |
| 위치 | {location} |
| 위험유형 | {risk_name} (`{risk_code}`) |
| 위험등급 | **{level}** (severity {severity}/3) |
| 위험점수 | {score:.2f} |

## 종합 판단 ({narr_src})
{summary}

## 판단 근거
{drv}

## 관측 지표
- 강수: {rain_mm_h:.1f} mm/h ({intensity})
- 평균 차량속도: {mean_speed:.1f} px/s{kmh} (감소율 {speed_drop * 100:.0f}%)
- 교통상태: {state} · 정체 대기열 {queue_len}대 · 정지차량 {stalled}대

## 권고 조치
**{recommendation}**

---
*본 보고서는 tot_dashboard 파이프라인이 자동 생성했습니다. 수치·시각·위치·위험등급·근거·권고는
규칙(RISK_CATALOG) 기반 **고정 필드**이며, 「종합 판단」 서술만 {narr_src}로 작성됩니다
(서술은 고정 필드를 변경하지 않음). ※ 교통신호 직접 제어는 수행하지 않으며 권고·통보까지만 제공합니다.*
"""


def build_briefing(decision: Decision, weather: WeatherState,
                   traffic: TrafficMetrics, location: str = "부산 도심 블록-A",
                   narrative: str | None = None) -> str:
    """Console-PoC path (object input). Uses ``narrative`` for the 종합 판단
    section if given, else falls back to ``decision.context``."""
    summary = narrative.strip() if narrative else decision.context
    return _render(
        risk_name=decision.risk_name, level=decision.level.value,
        severity=decision.severity, score=decision.score, t_sec=decision.t_sec,
        location=location, risk_code=decision.risk_code,
        rain_mm_h=weather.rain_mm_h, intensity=weather.intensity.value,
        mean_speed=traffic.mean_speed, speed_drop=traffic.speed_drop,
        state=traffic.state.value, queue_len=traffic.queue_len, stalled=traffic.stalled,
        drivers=decision.drivers, recommendation=decision.recommendation,
        summary=summary, narr_src=(AI_SRC if narrative else RULE_SRC))


def build_briefing_from_snapshot(snap: dict, narrative: str | None = None) -> str:
    """Dashboard path (store snapshot dict input). Falls back to the
    situation sentence / risk name if no narrative given."""
    summary = (narrative.strip() if narrative
               else (snap.get("situation_ko") or snap.get("risk_name", "")))
    return _render(
        risk_name=snap.get("risk_name", ""), level=snap.get("level", ""),
        severity=snap.get("severity", 0), score=snap.get("score", 0.0),
        t_sec=snap.get("t_sec", 0.0), location=snap.get("name", ""),
        risk_code=snap.get("risk_code", ""),
        rain_mm_h=snap.get("rain_mm_h", 0.0), intensity=snap.get("intensity", ""),
        mean_speed=snap.get("mean_speed", 0.0), speed_drop=snap.get("speed_drop", 0.0),
        mean_speed_kmh=snap.get("mean_speed_kmh"),
        state=snap.get("state", ""), queue_len=snap.get("queue_len", 0),
        stalled=snap.get("stalled", 0), drivers=snap.get("drivers"),
        recommendation=snap.get("recommendation", ""),
        summary=summary, narr_src=(AI_SRC if narrative else RULE_SRC))


def save_briefing(md: str, decision: Decision, out_dir: str = "data/reports") -> str:
    os.makedirs(out_dir, exist_ok=True)
    fname = f"briefing_t{decision.t_sec:.0f}s_{decision.risk_code}.md"
    path = os.path.join(out_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path
