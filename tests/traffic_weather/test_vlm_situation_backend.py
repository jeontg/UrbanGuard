"""교통위험 VlmSituationAgent × common/vlm.py 백엔드 연동 (Phase 6-A/6-A′,
2026-08-26).

전송 계층을 ``common/vlm.py``로 옮긴 뒤에도 이 에이전트의 겉 동작(폴백·
JSON 파싱·source 표기)이 그대로인지, 그리고 백엔드에 따라 ``last_source``
가 올바르게 갈리는지 확인한다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.common import vlm as VLM
from tot_dashboard.models import TrafficMetrics, TrafficState, WeatherIntensity, WeatherState
from tot_dashboard.traffic_weather.agents.vlm_situation import VlmSituationAgent


def _metrics(**over) -> TrafficMetrics:
    base = dict(t_sec=0.0, n_vehicles=3, mean_speed=40.0, speed_drop=0.1,
               density=0.2, queue_len=1, stalled=0, state=TrafficState.slow)
    base.update(over)
    return TrafficMetrics(**base)


def _weather(**over) -> WeatherState:
    base = dict(t_sec=0.0, rain_mm_h=0.0, intensity=WeatherIntensity.none)
    base.update(over)
    return WeatherState(**base)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TOT_VLM_BACKEND", "TOT_VLM_BASE_URL", "TOT_VLM_MODEL",
             "TOT_VLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_use_vlm이_꺼져있으면_규칙_폴백만_쓴다():
    agent = VlmSituationAgent(use_vlm=False)
    data = agent.interpret(None, _metrics(), _weather())
    assert data["source"] == "rule-fallback"
    assert agent.last_source == "rule-fallback"


def test_이미지가_없으면_규칙_폴백으로_떨어진다():
    """★ 회귀 방지 — VLM을 켰어도 이미지가 없으면(예: 스냅샷 실패)
    call_vlm 을 부르기 전에 먼저 걸러야 한다."""
    agent = VlmSituationAgent(use_vlm=True)
    data = agent.interpret(None, _metrics(), _weather())
    assert data["source"] == "rule-fallback"


def test_call_vlm이_성공하면_gemini_백엔드에서는_기존_source_문자열을_쓴다(monkeypatch):
    """★ 하위호환 — "gemini-vlm" 문자열 자체를 바꾸면 다른 화면·보고서가
    기대하는 값과 어긋난다."""
    monkeypatch.setattr(VLM, "call_vlm",
                        lambda *a, **k: '{"situation_ko": "테스트", "traffic_state": "서행"}')
    agent = VlmSituationAgent(use_vlm=True)
    data = agent.interpret(b"\xff\xd8\xff", _metrics(), _weather())
    assert data["source"] == "gemini-vlm"
    assert agent.last_source == "gemini-vlm"
    assert data["situation_ko"] == "테스트"


def test_openai_compatible_백엔드에서는_다른_source_문자열을_쓴다(monkeypatch):
    """화면에서 실제로 어느 백엔드를 탔는지 구분할 수 있어야 한다."""
    monkeypatch.setenv("TOT_VLM_BACKEND", "openai_compatible")
    monkeypatch.setattr(VLM, "call_vlm",
                        lambda *a, **k: '{"situation_ko": "사내 서버 응답"}')
    agent = VlmSituationAgent(use_vlm=True)
    data = agent.interpret(b"\xff\xd8\xff", _metrics(), _weather())
    assert data["source"] == "vlm-openai_compatible"
    assert agent.last_source == "vlm-openai_compatible"


def test_call_vlm이_None이면_규칙_폴백으로_떨어진다(monkeypatch):
    """fail-open — 호출 실패(키 없음·타임아웃·서버 오류 전부 None으로
    수렴)면 규칙 기반 서술로 이어져야 한다."""
    monkeypatch.setattr(VLM, "call_vlm", lambda *a, **k: None)
    agent = VlmSituationAgent(use_vlm=True)
    data = agent.interpret(b"\xff\xd8\xff", _metrics(state=TrafficState.congested),
                           _weather(rain_mm_h=10.0, intensity=WeatherIntensity.moderate))
    assert data["source"] == "rule-fallback"
    assert "강수" in data["situation_ko"] or "정체" in data["situation_ko"]


def test_깨진_json_응답도_규칙_폴백으로_떨어진다(monkeypatch):
    """VLM이 JSON이 아닌 텍스트를 돌려줘도(모델·서빙 스택마다 편차 있을 수
    있음, common/vlm.py 머리말 참고) 예외 없이 폴백해야 한다."""
    monkeypatch.setattr(VLM, "call_vlm", lambda *a, **k: "이건 JSON이 아닙니다")
    agent = VlmSituationAgent(use_vlm=True)
    data = agent.interpret(b"\xff\xd8\xff", _metrics(), _weather())
    assert data["source"] == "rule-fallback"


def test_narrate_report도_use_vlm_꺼지면_None():
    agent = VlmSituationAgent(use_vlm=False)
    assert agent.narrate_report({"location": "테스트"}) is None


def test_narrate_report은_call_vlm_결과를_그대로_돌려준다(monkeypatch):
    monkeypatch.setattr(VLM, "call_vlm", lambda *a, **k: "  종합 판단 문단입니다.  ")
    agent = VlmSituationAgent(use_vlm=True)
    assert agent.narrate_report({"location": "테스트"}) == "종합 판단 문단입니다."
