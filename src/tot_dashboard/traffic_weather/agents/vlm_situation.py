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
"""
from __future__ import annotations

import io
import json
import os

from ...models import TrafficState, WeatherIntensity

KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

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


def _to_jpeg_bytes(image, max_side: int = 768) -> bytes | None:
    """PIL.Image or JPEG bytes -> JPEG bytes."""
    if image is None:
        return None
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    try:
        im = image.copy()
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _extract_json(text) -> str:
    """Strip a ```json code fence / stray text, keep only the {...} span."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:].strip()
    s, e = raw.find("{"), raw.rfind("}")
    return raw[s:e + 1] if (s != -1 and e > s) else raw


class VlmSituationAgent:
    def __init__(self, use_vlm: bool = False, model: str = "gemini-2.5-flash",
                 location: str = "도심 블록"):
        self.use_vlm = use_vlm
        self.model = model
        self.location = location
        self.last_source = "rule-fallback"

    def interpret(self, image, traffic, weather, location: str | None = None) -> dict:
        if self.use_vlm:
            data = self._gemini(image, traffic, weather, location or self.location)
            if data is not None:
                self.last_source = "gemini-vlm"
                return data
        self.last_source = "rule-fallback"
        return self._fallback(traffic, weather)

    def _gemini(self, image, traffic, weather, location) -> dict | None:
        jpeg = _to_jpeg_bytes(image)
        key = _get_key()
        if jpeg is None or key is None:
            return None
        try:
            from google import genai
            from google.genai import types as gt
        except ImportError:
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
        try:
            client = genai.Client(api_key=key)
            cfg = dict(system_instruction=VLM_SYS_PROMPT, max_output_tokens=1024,
                       temperature=0.2, response_mime_type="application/json")
            try:
                # gemini-2.5-flash is a thinking model; unbounded thinking tokens
                # can crowd out the output so JSON gets truncated. Disable it.
                cfg["thinking_config"] = gt.ThinkingConfig(thinking_budget=0)
            except Exception:  # noqa: BLE001
                cfg["max_output_tokens"] = 2048  # older SDK: leave thinking-token headroom
            resp = client.models.generate_content(
                model=self.model,
                contents=[gt.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), user_text],
                config=gt.GenerateContentConfig(**cfg))
            data = json.loads(_extract_json(resp.text))
            data["source"] = "gemini-vlm"
            return data
        except Exception as e:  # noqa: BLE001
            print(f"  (Gemini call failed -> rule fallback): {str(e)[:140]}")
            return None

    def narrate_report(self, facts: dict, image=None) -> str | None:
        """Generate only the briefing's "종합 판단" narrative paragraph from
        fixed facts (numeric/categorical fields are rule-derived elsewhere and
        never touched here). Returns None on missing key/module/failure so the
        caller falls back to a rule-based sentence."""
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
        parts = []
        jpeg = _to_jpeg_bytes(image)
        if jpeg is not None:
            parts.append(gt.Part.from_bytes(data=jpeg, mime_type="image/jpeg"))
        parts.append(facts_text)
        try:
            client = genai.Client(api_key=key)
            cfg = dict(system_instruction=REPORT_SYS_PROMPT, max_output_tokens=800,
                       temperature=0.3)
            try:
                cfg["thinking_config"] = gt.ThinkingConfig(thinking_budget=0)
            except Exception:  # noqa: BLE001
                cfg["max_output_tokens"] = 1600
            resp = client.models.generate_content(
                model=self.model, contents=parts,
                config=gt.GenerateContentConfig(**cfg))
            return (resp.text or "").strip() or None
        except Exception as e:  # noqa: BLE001
            print(f"  (report narrative Gemini call failed -> rule sentence): {str(e)[:140]}")
            return None

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
