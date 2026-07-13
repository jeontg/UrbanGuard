"""Crowd-domain VLM interpretation — control-center crowd warning text.

Ported from SAM's ``SAM3_install.py`` (Stage 3). Same Gemini-call mechanics as
``traffic_weather.agents.vlm_situation`` (this is in fact the direction the
pattern was copied — SAM's ``vlm_interpret`` was the original, retargeted for
traffic in flood3; see docs/integration_plan.md section 2) but with its own
system prompt and rule-based fallback tuned for crowd density/movement rather
than traffic/rain. Kept as a separate module rather than sharing one generic
"VLM client" abstraction with traffic_weather, since the two prompts/fallback
rule sets are large and domain-specific enough that a shared wrapper would
mostly just be the ``google-genai`` call boilerplate.

The rule-based fallback (used whenever no Gemini key/module/image is
available, or the call fails) needs no GPU and is fully testable on CPU.
"""
from __future__ import annotations

import io
import json
import os

from .config import CCTV_ID, GEMINI_MODEL, KEY_NAMES, LOCATION_NAME

VLM_SYS_PROMPT = """당신은 야간 상업지구 통합관제센터의 군중안전 분석 AI다.
CCTV 정지영상과 정량지표(인원·화면밀집도·이동속도·방향분산도·발산도)를 받아,
관제요원·현장 경찰/소방·시민 안내방송·지자체 보고에 즉시 활용 가능한
'군중 밀도 및 이동 경고'를 생성한다.

[경고 등급] 행정안전부 위기경보 4단계를 사용한다.
  관심(Blue) -> 주의(Yellow) -> 경계(Orange) -> 심각(Red)
  내부 위험유형 매핑:
   - 정상/안정                  -> 관심
   - 군중 밀집 상승             -> 주의
   - 이동흐름 혼란(역류·병목)   -> 주의~경계
   - 군중 급증 위험             -> 경계
   - 패닉성 급속 분산/압사우려  -> 심각

[밀도 표현 규칙] 숫자(%, 명 등 원시수치)를 문장에 직접 쓰지 말고,
  보행밀도 LOS(명/㎡) 기준을 '정성적 등급'으로 환산해 표현한다.
   여유(~1명/㎡) · 보통(1~2) · 다소혼잡(2~3) · 혼잡(3~4) ·
   매우혼잡(4~5, 위험) · 극도혼잡(5명/㎡~, 압사우려)
  이미지로 단위면적당 인원을 추정하되 근거가 약하면 '추정' 표현을 쓴다.

[이동 표현 규칙] 전문용어 대신 쉬운 일상어로 쓴다.
  특히 '역류(반대방향 밀침)'와 '병목(좁은 곳 정체)'을 우선 식별해 명시한다.
  예: "사람들이 서로 반대로 밀리고 있습니다", "출구 앞이 막혀 정체됩니다"

[작성 지침]
  - 톤: 차분하고 객관적인 서술형. 과장 금지.
  - 위치: 반드시 구역명으로 지칭(좌표·픽셀 금지). 미상이면 '해당 구역'.
  - 시간: 야간 환경(저조도·조명) 가정.
  - 길이: 등급이 낮으면 1문장, 높을수록 길게(최대 3~4문장).
  - 정상(관심)일 때는 '위험 없음'을 분명히 밝힌다(예: '위험 없음 — 특이사항 없음').
  - '압사 위험'·'대피' 같은 극단적 표현은 '심각' 등급에서만 사용한다.
  - 불확실하면 단정하지 말고 '추정/가능성' 표현을 쓴다.
  - 경고문(warning_ko/en)에는 권고조치·수치·신뢰도를 넣지 않는다.
  - 권고조치는 recommended_action 필드에만(현장·지자체용).

[출력 형식] 반드시 아래 JSON 한 개만 출력한다(설명 금지):
{
  "level": "관심|주의|경계|심각",
  "level_en": "Blue|Yellow|Orange|Red",
  "risk_type": "정상|군중밀집|이동흐름혼란|군중급증위험|패닉분산",
  "density_grade": "여유|보통|다소혼잡|혼잡|매우혼잡|극도혼잡",
  "movement": "역류|병목|정체|발산|정상 중 해당(쉬운말 보조설명)",
  "warning_ko": "관제·방송용 한국어 경고문(차분 서술형, 수치·조치 제외)",
  "warning_en": "위 경고문의 영어 번역",
  "recommended_action": "현장/지자체용 권고조치(한국어, 간결)",
  "escalation": "조건부 상향 안내(예: '3분 내 개선 없으면 경계->심각'). 없으면 빈 문자열",
  "confidence": "고|중|저",
  "uncertainty_note": "추정 근거/한계(없으면 빈 문자열)"
}"""

LAST_VLM: dict = {}


def _get_google_key() -> str | None:
    for n in KEY_NAMES:
        if os.environ.get(n):
            return os.environ[n]
    return None


def _pil_to_jpeg_bytes(pil_image, max_side: int = 768) -> bytes:
    im = pil_image.copy()
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def vlm_interpret(pil_image, sam3_summary: dict, behavior: dict, prompt_text: str = "person",
                  location: str | None = None, cctv_id: str | None = None) -> tuple[str, str]:
    """SAM3 result + behavior metrics -> control-center warning text.
    Returns (warning_ko, source); the full structured response is in
    ``LAST_VLM``."""
    global LAST_VLM
    loc = location or LOCATION_NAME
    user_text = (
        f"[구역] {loc} (CCTV {cctv_id or CCTV_ID}), 시간대: 야간\n"
        f"[정량지표] 감지대상='{prompt_text}', 인원={sam3_summary['count']}명, "
        f"화면밀집도={sam3_summary['density_pct']:.1f}%, "
        f"평균이동속도={behavior['mean_speed']:.1f}px/s, "
        f"속도급증비={behavior['surge']:.2f}, "
        f"방향분산도={behavior['dispersion']:.2f}, "
        f"발산도={behavior['divergence']:+.1f}\n"
        f"위 장면과 지표로 군중 밀도·이동 경고를 JSON으로 생성하라."
    )
    api_key = _get_google_key()
    if api_key and pil_image is not None:
        try:
            from google import genai
            from google.genai import types as genai_types
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    genai_types.Part.from_bytes(
                        data=_pil_to_jpeg_bytes(pil_image), mime_type="image/jpeg"),
                    user_text,
                ],
                config=genai_types.GenerateContentConfig(
                    system_instruction=VLM_SYS_PROMPT,
                    max_output_tokens=600, temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(resp.text)
            data["_meta"] = {"location": loc, "cctv_id": cctv_id or CCTV_ID, "model": GEMINI_MODEL}
            LAST_VLM = data
            return data.get("warning_ko", "").strip() or "(경고문 없음)", "gemini-vlm"
        except Exception as e:  # noqa: BLE001
            print(f"  (Gemini API 실패 -> 폴백): {str(e)[:160]}")

    # rule-based fallback (offline)
    d = sam3_summary["density_pct"]
    sg = behavior["surge"]
    dp = behavior["dispersion"]
    dv = behavior["divergence"]
    if dv > 6 and sg > 1.3:
        lvl, rt, grade = "심각", "패닉분산", "매우혼잡"
    elif sg > 1.6:
        lvl, rt, grade = "경계", "군중급증위험", "혼잡"
    elif dp > 0.6:
        lvl, rt, grade = "주의", "이동흐름혼란", "다소혼잡"
    elif d > 40:
        lvl, rt, grade = "주의", "군중밀집", "혼잡"
    else:
        lvl, rt, grade = "관심", "정상", "보통"
    move = ("역류·병목" if rt == "이동흐름혼란" else "급속 분산" if rt == "패닉분산" else "정상")
    ko_map = {
        "관심": f"{loc} 위험 없음. 군중 흐름이 안정적이며 특이사항 없습니다.",
        "주의": f"{loc} 일대 인파가 늘며 다소 혼잡합니다. 이동 흐름을 지켜봐 주십시오.",
        "경계": f"{loc}에서 사람들의 이동이 갑자기 빨라지며 혼잡이 심해지고 있습니다.",
        "심각": f"{loc}에서 군중이 급격히 흩어지며 위험한 움직임이 보입니다. 즉시 대응이 필요합니다.",
    }
    ko = ko_map[lvl]
    LAST_VLM = {"level": lvl, "risk_type": rt, "density_grade": grade, "movement": move,
                "warning_ko": ko, "warning_en": "(rule-based, no translation)",
                "recommended_action": "", "escalation": "", "confidence": "중",
                "uncertainty_note": "규칙기반 추정",
                "_meta": {"location": loc, "cctv_id": cctv_id or CCTV_ID, "model": "rule-based"}}
    return ko, "rule-based"
