"""도로 노면 손상 보고서용 「종합 판단」 서술 생성기.

``traffic_weather/agents/vlm_situation.py``와 동일한 패턴(Gemini 선택적 호출
+ 규칙 기반 폴백, 등급·손상목록 등 고정 필드는 절대 건드리지 않고 서술만
생성)을 따르되, 프롬프트는 도로 노면 손상 전용으로 새로 작성했다.

⚠ 현재 도로 노면 손상 데이터는 전부 mock(모의)이다(road/mock_data.py 참고).
실제 카메라 이미지를 함께 보내 AI가 "화면에서 확인한 것처럼" 서술하게 하면
가짜 손상과 실제 화면이 어긋나 오히려 오해를 부를 수 있어, 의도적으로 이미지
없이 텍스트 사실만으로 서술한다. 실제 탐지 모델이 붙으면(Phase 3+) flood처럼
스냅샷 이미지를 함께 전달하도록 확장할 수 있다.
"""
from __future__ import annotations

import os

KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

ROAD_REPORT_SYS_PROMPT = """당신은 도로 노면 관리 상황실의 보고서 작성 AI다.
아래에 '확정된 사실'(위치·등급·손상 목록)이 주어진다. 이 사실만 근거로,
도로관리과가 즉시 읽을 수 있는 '종합 판단' 서술을 작성한다.

[작성 규칙]
 - 주어진 등급·손상 목록을 바꾸거나, 없는 손상·수치를 지어내지 말 것(환각 금지).
 - 손상 유형·개수·심각도를 근거로 보수 시급성만 판단해 서술한다.
 - 신호 통제 등 도로 노면 관리와 무관한 조치를 지어내지 말 것.
 - 객관적·간결한 보고문체, 2~4문장.
 - 마크다운 머리말·목록 없이 '문단 텍스트'만 출력한다."""


def _get_key() -> str | None:
    for n in KEY_NAMES:
        if os.environ.get(n):
            return os.environ[n]
    try:
        from dotenv import load_dotenv
        load_dotenv()
        for n in KEY_NAMES:
            if os.environ.get(n):
                return os.environ[n]
    except Exception:  # noqa: BLE001
        pass
    return None


class RoadSituationAgent:
    def __init__(self, use_vlm: bool = True, model: str = "gemini-2.5-flash"):
        self.use_vlm = use_vlm
        self.model = model

    def narrate_report(self, facts: dict) -> str | None:
        """텍스트 사실만으로 '종합 판단' 서술 생성.

        키 미설정/모듈 없음/호출 실패 시 None을 반환하며, 호출자는 규칙 기반
        문장으로 폴백해야 한다(``road/report_generator.py`` 참고).
        """
        if not self.use_vlm:
            return None
        key = _get_key()
        if key is None:
            return None
        try:
            from google import genai
            from google.genai import types as gt
        except ImportError:
            return None

        defects_text = "\n".join(
            f"- {d['type']} (등급 {d['grade']} {d['grade_label']}, "
            f"신뢰도 {d['confidence'] * 100:.0f}%, {d['detected_minutes_ago']}분 전 탐지)"
            for d in facts.get("defects", [])
        ) or "없음"
        facts_text = (
            f"[위치] {facts.get('name')}\n"
            f"[종합 등급] {facts.get('grade_label')} (등급 {facts.get('grade')}/4)\n"
            f"[탐지된 손상 {len(facts.get('defects', []))}건]\n{defects_text}\n"
            "위 확정 사실만으로 '종합 판단' 서술을 작성하라(문단 텍스트만)."
        )
        try:
            client = genai.Client(api_key=key)
            cfg = dict(system_instruction=ROAD_REPORT_SYS_PROMPT, max_output_tokens=600,
                       temperature=0.3)
            try:
                cfg["thinking_config"] = gt.ThinkingConfig(thinking_budget=0)
            except Exception:  # noqa: BLE001
                cfg["max_output_tokens"] = 1200
            resp = client.models.generate_content(
                model=self.model, contents=[facts_text],
                config=gt.GenerateContentConfig(**cfg))
            return (resp.text or "").strip() or None
        except Exception as e:  # noqa: BLE001
            print(f"  (road report narrative Gemini call failed -> rule sentence): {str(e)[:140]}")
            return None
