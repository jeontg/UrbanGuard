"""VLM Situation Agent (interpretation stage).

Ported from flood3's ``agents/vlm_situation.py``. Reads CCTV context +
quantitative traffic/weather metrics and produces a "congestion likely due to
rain" style situation description. Pattern ported from SAM's SAM3
``vlm_interpret`` (Gemini + strict JSON system prompt + rule-based fallback) —
see docs/integration_plan.md section 2 for this cross-project lineage; SAM's
crowd-domain version is ported separately in Phase 5 (``crowd/``) since the
prompts/fallback rules are domain-specific even though the Gemini-call
mechanics are the same pattern.

If the API key / ``google-genai`` / image are not all available, or the call
fails, falls back to a deterministic rule-based description — the pipeline
always produces *something*.

★ 2026-08-26 — 전송 계층(키 조회·JPEG 인코딩·타임아웃·JSON 추출)은
``common/vlm.py``로 옮겼다(Phase 6-A). 이 파일에는 프롬프트와 규칙 기반
폴백만 남긴다 — 그 둘은 도메인 고유라 공용화하지 않는다.
``TOT_VLM_BACKEND=openai_compatible``로 두면 클라우드 Gemini 대신 관제망
**내부**의 OpenAI 호환 서버(vLLM·Ollama 등)를 부른다 — ``common/vlm.py``
머리말 참고.
"""
from __future__ import annotations

import json
import logging

from ...common import vlm as VLM
from ...models import TrafficState, WeatherIntensity

log = logging.getLogger("urbanguard.traffic.vlm_situation")

VLM_SYS_PROMPT = """당신은 도심 교통·재난 통합관제센터의 도로상황 분석 AI다.
도로 CCTV 정지영상과 정량지표(차량수·평균속도·속도감소율·정체대기열·정지차량·강수량)를
받아, 관제요원·도로관리부서·상황보고에 즉시 활용 가능한 '도로 상황 해석'을 생성한다.

[해석 지침]
 - 강수(기상)와 교통 정체의 '연관성'을 우선 판단한다. 강수가 있고 속도가 크게
   감소했으면 '강수로 인한 정체'로 추정하되, 단정하지 말고 '추정' 표현을 쓴다.
 - 강수가 없는데 정체면 '기상영향 아님(일반 정체 가능)'으로 구분한다.
 - 톤: 차분하고 객관적인 서술형. 과장 금지. 길이는 1~3문장.
 - 위치는 구역명으로 지칭(좌표·픽셀 금지).
 - 침수·통행불가 같은 극단 표현은 정지차량이 다수이고 강수가 강할 때만 쓴다.
 - 신호 제어·통제 같은 '조치'는 권고 필드(recommended_action)에만, 짧게.

[출력 형식] 반드시 아래 JSON 한 개만 출력한다(설명 금지):
{
  "situation_ko": "관제용 한국어 상황 서술(1~3문장, 차분 서술형)",
  "traffic_state": "원활|서행|정체|정지",
  "weather_relation": "강수 연관 추정|무관",
  "movement": "원활|서행|정체|정지 중 해당(쉬운말 보조설명)",
  "recommended_action": "도로부서/관제용 권고(간결, 신호 직접제어 금지)",
  "confidence": "고|중|저",
  "uncertainty_note": "추정 근거/한계(없으면 빈 문자열)"
}"""

REPORT_SYS_PROMPT = """당신은 도심 교통·재난 통합관제센터의 상황 보고서 작성 AI다.
아래에 '확정된 사실(수치·위험등급·근거·권고)'이 주어진다. 이 사실만 근거로,
관제 상황실·유관부서가 즉시 읽을 수 있는 '사건 브리핑 종합 판단' 서술을 작성한다.

[작성 규칙]
 - 주어진 수치·위험등급·권고를 바꾸거나, 없는 수치·지시를 지어내지 말 것(환각 금지).
 - 사실들을 매끄럽게 '연결·해석'만 한다(무슨 일이·왜·어떤 영향인지).
 - 강수와 교통 정체의 인과는 '추정' 표현으로(단정 금지).
 - 신호 직접제어 등 권고에 없는 조치를 새로 만들지 말 것.
 - 위치·구역명으로 지칭(픽셀·좌표 금지). 객관적·간결한 보고문체, 3~6문장.
 - 마크다운 머리말·목록 없이 '문단 텍스트'만 출력한다."""


class VlmSituationAgent:
    def __init__(self, use_vlm: bool = False, model: str = "gemini-2.5-flash",
                 location: str = "도심 블록"):
        self.use_vlm = use_vlm
        self.model = model
        self.location = location
        self.last_source = "rule-fallback"

    def interpret(self, image, traffic, weather, location: str | None = None) -> dict:
        if self.use_vlm:
            data = self._call_vlm(image, traffic, weather, location or self.location)
            if data is not None:
                # ★ 2026-08-26 — 백엔드에 따라 출처를 다르게 남긴다.
                #   "gemini-vlm"은 기존 값 그대로 유지(하위호환·회귀 방지),
                #   사내 서버는 새 값으로 구분해 화면에서 실제로 어느
                #   쪽을 탔는지 알 수 있게 한다.
                self.last_source = ("gemini-vlm" if VLM.backend() == "gemini"
                                    else "vlm-openai_compatible")
                return data
        self.last_source = "rule-fallback"
        return self._fallback(traffic, weather)

    def _call_vlm(self, image, traffic, weather, location) -> dict | None:
        jpeg = VLM.to_jpeg_bytes(image)
        if jpeg is None:
            return None
        user_text = (
            f"[구역] {location} 도로 CCTV\n"
            f"[정량지표] 차량수={traffic.n_vehicles}대, "
            f"평균속도={traffic.mean_speed:.0f}px/s, "
            f"속도감소율={traffic.speed_drop * 100:.0f}%, "
            f"정체대기열={traffic.queue_len}대, 정지차량={traffic.stalled}대, "
            f"강수량={weather.rain_mm_h:.0f}mm/h({weather.intensity.value})\n"
            f"위 장면과 지표로 도로 상황 해석을 JSON으로 생성하라."
        )
        text = VLM.call_vlm(VLM_SYS_PROMPT, user_text, jpeg, model=self.model)
        if text is None:
            return None
        try:
            data = json.loads(VLM.extract_json(text))
        except Exception as e:  # noqa: BLE001
            log.warning("VLM 응답 JSON 파싱 실패 -> 규칙 폴백: %s", str(e)[:140])
            return None
        data["source"] = "gemini-vlm" if VLM.backend() == "gemini" else "vlm-openai_compatible"
        return data

    def narrate_report(self, facts: dict, image=None) -> str | None:
        """Generate only the briefing's "종합 판단" narrative paragraph from
        fixed facts (numeric/categorical fields are rule-derived elsewhere and
        never touched here). Returns None on missing key/module/failure so the
        caller falls back to a rule-based sentence."""
        if not self.use_vlm:
            return None
        facts_text = (
            f"[위치] {facts.get('location')}\n"
            f"[영상시각] {facts.get('t_sec')}s\n"
            f"[위험유형] {facts.get('risk_name')} ({facts.get('risk_code')})\n"
            f"[위험등급] {facts.get('level')} "
            f"(severity {facts.get('severity')}/3, 점수 {facts.get('score')})\n"
            f"[강수] {facts.get('rain_mm_h')}mm/h ({facts.get('intensity')})\n"
            f"[교통] 평균속도 {facts.get('mean_speed')}px/s, 감속 {facts.get('speed_drop')}%, "
            f"상태 {facts.get('state')}, 정체대기열 {facts.get('queue_len')}대, "
            f"정지차량 {facts.get('stalled')}대\n"
            f"[근거] {', '.join(facts.get('drivers') or [])}\n"
            f"[권고(고정)] {facts.get('recommendation')}\n"
            f"위 확정 사실만으로 '종합 판단' 서술을 작성하라(문단 텍스트만)."
        )
        jpeg = VLM.to_jpeg_bytes(image)
        text = VLM.call_vlm(REPORT_SYS_PROMPT, facts_text, jpeg, model=self.model,
                            temperature=0.3, max_output_tokens=800, json_mode=False)
        return (text or "").strip() or None

    def _fallback(self, traffic, weather) -> dict:
        raining = weather.intensity != WeatherIntensity.none
        st = traffic.state
        if st == TrafficState.free:
            s = "차량 흐름 원활 — 특이사항 없음"
        elif st == TrafficState.slow:
            s = "차량 흐름이 다소 느려짐" + (" (강우 영향 추정)" if raining else "")
        elif st == TrafficState.congested:
            s = "정체 발생" + (" — 강수로 인한 정체로 추정" if raining else "")
        else:
            s = "차량이 거의 멈춤 — " + ("침수·통행불가 가능성" if raining else "정체 심각")
        return {
            "situation_ko": s,
            "traffic_state": st.value,
            "weather_relation": "강수 연관 추정" if (raining and st != TrafficState.free) else "무관",
            "movement": st.value,
            "recommended_action": "",
            "confidence": "중",
            "uncertainty_note": "",
            "source": "rule-fallback",
        }
