"""VLM 호출 전송 계층 — Phase 6-A/6-A′ (2026-08-26).

**왜 있는가.** `traffic_weather/agents/vlm_situation.py`·`crowd/vlm_situation.py`·
`road/situation_agent.py` 세 곳이 각자 「키 조회 → JPEG 인코딩 → Gemini 호출
→ JSON 추출」을 거의 그대로 복제해 왔다(`crowd/vlm_situation.py` 머리말에
「공용 래퍼는 보일러플레이트뿐일 것」이라는 반대 의견이 실제로 남아 있고,
**프롬프트·폴백에 대해서는 지금도 옳다** — 그래서 여기서는 전송 계층만
뽑고, 프롬프트·규칙 기반 폴백은 각 도메인 파일에 그대로 둔다).

**왜 지금 손대는가.** 두 가지가 새로 드러났다.

1. **타임아웃이 어디에도 없었다.** 폐쇄망에서 DNS 블랙홀이면 탐지 스레드가
   통째로 묶인다(`detection_source.py`가 FFmpeg에서 겪은 것과 같은 부류).
2. **폐쇄망에서 클라우드 Gemini에 못 닿을 수 있다**(2026-08-26 사용자 질의
   「외부 VLM API 연동이... 내부에서 작동되는 방법은 없나요?」). 그래서
   ``TOT_VLM_BACKEND`` 로 **자체 호스팅(OpenAI 호환) 서버**로도 부를 수
   있게 백엔드를 추상화한다.

**설계 원칙 — 실패는 전부 ``None`` 으로 수렴한다.** 키가 없거나, SDK가
없거나, 이미지가 없거나, 타임아웃이 나거나, 서버가 오류를 내거나 — 전부
같다. 호출부가 그 값을 보고 **규칙 기반 폴백으로 이어지게** 하기 위해서다
(fail-open). 이 모듈은 그 판단을 대신하지 않는다 — 「VLM을 못 썼다」는
사실만 돌려주고, 「그래서 어떻게 할지」는 도메인 코드가 정한다.

**백엔드 선택**
    ``TOT_VLM_BACKEND`` (기본 ``"gemini"``)
        - ``"gemini"`` — ``google-genai`` SDK, 클라우드 Gemini. **인터넷 필수.**
        - ``"openai_compatible"`` — ``openai`` SDK, ``TOT_VLM_BASE_URL`` 이
          가리키는 서버. 그 서버가 관제망 **내부**(vLLM·Ollama로 띄운
          오픈소스 VLM 등)라면 **인터넷 연결이 전혀 필요 없다.**

⚠️ **이번 회차는 코드 구조만 준비한다** — 실제 사내 GPU 서버·오픈소스 VLM
모델은 아직 없다(사용자 확정, 2026-08-26). ``openai_compatible`` 경로는
목(mock) 서버로만 시험됐다 — 실제 서버를 붙일 때
``response_format=json_object`` 지원 여부·한국어 출력 품질·응답 지연을
다시 확인해야 한다.
"""
from __future__ import annotations

import base64
import io
import logging
import os

log = logging.getLogger("urbanguard.vlm")

KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def backend() -> str:
    """지금 설정된 백엔드. 모르는 값이 들어와도 예외를 내지 않고
    ``"gemini"``(기존 동작)로 되돌아간다 — 오타 하나로 탐지가 멈추면 안 된다."""
    v = (os.environ.get("TOT_VLM_BACKEND") or "gemini").strip().lower()
    return v if v in ("gemini", "openai_compatible") else "gemini"


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


def to_jpeg_bytes(image, max_side: int = 768) -> bytes | None:
    """PIL.Image 또는 JPEG bytes -> JPEG bytes. 이미 bytes면 그대로 통과."""
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


def extract_json(text) -> str:
    """```json 코드펜스나 잡담을 걷어내고 {...} 구간만 남긴다."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:].strip()
    s, e = raw.find("{"), raw.rfind("}")
    return raw[s:e + 1] if (s != -1 and e > s) else raw


def call_vlm(system_prompt: str, user_text: str, image_bytes: bytes | None, *,
            model: str | None = None, timeout_sec: float = 8.0,
            temperature: float = 0.2, max_output_tokens: int = 1024,
            json_mode: bool = True) -> str | None:
    """VLM을 한 번 호출한다. 성공하면 원문 텍스트(보통 JSON 문자열),
    실패하면 ``None``.

    :param model: 비우면 백엔드 기본값(``gemini`` 는
        :data:`DEFAULT_GEMINI_MODEL`, ``openai_compatible`` 은
        ``TOT_VLM_MODEL`` 환경변수)을 쓴다.
    :param timeout_sec: 왕복 제한 시간. **어느 백엔드든 반드시 지킨다** —
        폐쇄망 DNS 블랙홀·사내 서버 다운 모두 이 값 안에서 포기해야
        탐지 스레드가 묶이지 않는다.
    :param json_mode: True면 백엔드가 JSON 형식 응답을 강제하도록 요청한다
        (지원 안 하는 서버도 있을 수 있음 — 실패하면 그대로 None).
    """
    if backend() == "openai_compatible":
        return _call_openai_compatible(
            system_prompt, user_text, image_bytes, model=model,
            timeout_sec=timeout_sec, temperature=temperature,
            max_output_tokens=max_output_tokens, json_mode=json_mode)
    return _call_gemini(
        system_prompt, user_text, image_bytes, model=model,
        timeout_sec=timeout_sec, temperature=temperature,
        max_output_tokens=max_output_tokens, json_mode=json_mode)


def _call_gemini(system_prompt, user_text, image_bytes, *, model, timeout_sec,
                 temperature, max_output_tokens, json_mode) -> str | None:
    key = _get_key()
    if key is None:
        return None
    try:
        from google import genai
        from google.genai import types as gt
    except ImportError:
        return None
    try:
        # ★ 2026-08-26 — 타임아웃 신설(그전까지 전무). HttpOptions.timeout은
        #   밀리초 단위다(SDK 필드 설명으로 확인).
        client = genai.Client(
            api_key=key,
            http_options=gt.HttpOptions(timeout=int(timeout_sec * 1000)))
        cfg = dict(system_instruction=system_prompt,
                   max_output_tokens=max_output_tokens, temperature=temperature)
        if json_mode:
            cfg["response_mime_type"] = "application/json"
        try:
            # gemini-2.5-flash 는 사고형(thinking) 모델이라, thinking 토큰이
            # 무제한이면 출력 JSON이 잘릴 수 있다 — 꺼 둔다.
            cfg["thinking_config"] = gt.ThinkingConfig(thinking_budget=0)
        except Exception:  # noqa: BLE001
            cfg["max_output_tokens"] = max_output_tokens * 2  # 구버전 SDK 대비 여유

        parts: list = []
        if image_bytes is not None:
            parts.append(gt.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        parts.append(user_text)

        resp = client.models.generate_content(
            model=model or DEFAULT_GEMINI_MODEL, contents=parts,
            config=gt.GenerateContentConfig(**cfg))
        return resp.text
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini VLM 호출 실패 -> 폴백: %s", str(e)[:140])
        return None


def _call_openai_compatible(system_prompt, user_text, image_bytes, *, model,
                            timeout_sec, temperature, max_output_tokens,
                            json_mode) -> str | None:
    """OpenAI 호환 Chat Completions 서버 호출 — vLLM·Ollama 등으로 관제망
    **내부**에 띄운 오픈소스 VLM을 겨냥한다. ``TOT_VLM_BASE_URL`` 만 그
    내부 주소로 두면 인터넷 연결이 전혀 필요 없다."""
    base_url = (os.environ.get("TOT_VLM_BASE_URL") or "").strip()
    if not base_url:
        return None
    model = (model or os.environ.get("TOT_VLM_MODEL") or "").strip()
    if not model:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None

    content: list = [{"type": "text", "text": user_text}]
    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    messages = [{"role": "system", "content": system_prompt},
               {"role": "user", "content": content}]

    try:
        # 로컬 서버는 보통 인증이 없다 — 키를 안 요구하는 서버도 openai SDK가
        # 빈 문자열을 거부하므로 더미 값을 채운다.
        api_key = (os.environ.get("TOT_VLM_API_KEY") or "").strip() or "not-needed"
        # max_retries=0 — SDK 기본(2회 재시도)이면 사내 서버가 다운됐을 때
        # timeout_sec의 최대 3배까지 걸릴 수 있다. 이 프로젝트의 호출
        # 빈도(카메라별 최소 60초 간격)에서는 재시도 이득보다 "빨리 포기하고
        # 규칙 폴백으로 넘어가는 것"이 fail-open 원칙에 더 맞는다.
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_sec,
                        max_retries=0)
        kwargs = dict(model=model, messages=messages, temperature=temperature,
                     max_tokens=max_output_tokens)
        if json_mode:
            # ⚠️ 모든 서빙 스택이 이 필드를 지키지는 않는다(실측 필요,
            #   모듈 머리말 참고) — 무시하는 서버는 그냥 텍스트로 응답하고,
            #   호출부의 JSON 파싱이 실패하면 그것도 결국 규칙 기반
            #   폴백으로 이어진다(fail-open 원칙 유지).
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content
    except Exception as e:  # noqa: BLE001
        log.warning("사내 VLM(openai_compatible) 호출 실패 -> 폴백: %s", str(e)[:140])
        return None
