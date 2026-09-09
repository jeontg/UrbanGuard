"""VLM 호출 전송 계층(``common/vlm.py``) — Phase 6-A/6-A′, 2026-08-26.

`docs/pending_tasks.md` §Phase 6 참고 — 사용자 질의(「외부 VLM API 연동이
외부와 작동되는데 내부에서 작동되는 방법은 없나요?」)에 대한 응답으로
백엔드를 ``TOT_VLM_BACKEND``로 고를 수 있게 했다.

지켜야 할 것.

* **기본 백엔드는 gemini** — 기존 동작과 100% 동일해야 한다(회귀 방지가
  최우선). 모르는 값이 들어와도 예외 없이 gemini로 되돌아간다
* **`openai_compatible` 백엔드는 google-genai를 전혀 건드리지 않는다** —
  폐쇄망에서 그 SDK가 인터넷에 나가려 시도해서는 안 된다
* **실패는 전부 None으로 수렴한다(fail-open)** — 키 없음·URL 없음·모델
  없음·SDK 없음·타임아웃 전부 같다
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tot_dashboard.common import vlm as VLM


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TOT_VLM_BACKEND", "TOT_VLM_BASE_URL", "TOT_VLM_MODEL",
             "TOT_VLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# --- 백엔드 선택 ---------------------------------------------------------------


def test_기본_백엔드는_gemini다():
    assert VLM.backend() == "gemini"


def test_모르는_백엔드값은_gemini로_되돌아간다(monkeypatch):
    """오타 하나로 탐지가 멈추면 안 된다."""
    monkeypatch.setenv("TOT_VLM_BACKEND", "이상한값")
    assert VLM.backend() == "gemini"


def test_openai_compatible로_설정하면_백엔드가_바뀐다(monkeypatch):
    monkeypatch.setenv("TOT_VLM_BACKEND", "openai_compatible")
    assert VLM.backend() == "openai_compatible"


def test_대소문자나_앞뒤공백은_가려서_읽는다(monkeypatch):
    monkeypatch.setenv("TOT_VLM_BACKEND", "  OpenAI_Compatible  ")
    assert VLM.backend() == "openai_compatible"


# --- 백엔드 격리 (기존 회귀 방지) ------------------------------------------------


def test_gemini_백엔드일때_openai_compatible_경로를_타지_않는다(monkeypatch):
    """★ 회귀 방지 — 기본 백엔드는 지금까지와 똑같이 동작해야 한다."""
    def _boom(*a, **k):
        raise AssertionError("gemini 백엔드인데 openai_compatible 경로가 불렸다")
    monkeypatch.setattr(VLM, "_call_openai_compatible", _boom)
    # 키가 없으므로 _call_gemini 는 조용히 None을 돌려준다(google-genai를
    # 실제로 부르지 않는다) — SDK 미설치 환경에서도 이 시험이 흔들리지
    # 않도록 키 부재로 짧게 끊는다.
    assert VLM.call_vlm("sys", "user", None) is None


def test_openai_compatible_백엔드는_gemini_경로를_타지_않는다(monkeypatch):
    """★ 핵심 — 폐쇄망에서 google-genai(외부 SDK)가 전혀 관여하면 안 된다."""
    monkeypatch.setenv("TOT_VLM_BACKEND", "openai_compatible")

    def _boom(*a, **k):
        raise AssertionError("openai_compatible 백엔드인데 gemini(google-genai) 경로가 불렸다")
    monkeypatch.setattr(VLM, "_call_gemini", _boom)
    # base_url이 없으므로 _call_openai_compatible 자체도 조용히 None을
    # 돌려준다 — 그 과정에서 _call_gemini 로 새지 않는지가 이 시험의 요점.
    assert VLM.call_vlm("sys", "user", None) is None


# --- openai_compatible 백엔드 — 설정 누락 ----------------------------------------


def test_base_url이_없으면_None(monkeypatch):
    monkeypatch.setenv("TOT_VLM_BACKEND", "openai_compatible")
    monkeypatch.setenv("TOT_VLM_MODEL", "test-model")
    assert VLM.call_vlm("sys", "user", None) is None


def test_model이_없으면_None(monkeypatch):
    monkeypatch.setenv("TOT_VLM_BACKEND", "openai_compatible")
    monkeypatch.setenv("TOT_VLM_BASE_URL", "http://127.0.0.1:1/v1")
    assert VLM.call_vlm("sys", "user", None) is None


# --- fail-open: 실제 느린 서버로 타임아웃 확인 ------------------------------------


class _SlowHandler(BaseHTTPRequestHandler):
    """요청을 받고도 오래 침묵한다 — 사내 서버가 응답 없을 때를 흉내낸다."""

    def do_POST(self):  # noqa: N802
        time.sleep(20.0)  # 아래 시험의 타임아웃(1초)보다 훨씬 길게 지연
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"choices":[{"message":{"content":"{}"}}]}')

    def log_message(self, *args):  # noqa: D401 — 시험 출력 조용히
        pass


@pytest.fixture
def slow_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    # 요청 처리 스레드(20초 sleep 중)가 프로세스 종료를 막지 않게 한다 —
    # ThreadingMixIn 은 기본으로 non-daemon 스레드를 쓴다.
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    t.join(timeout=2)


def test_로컬_서버가_느리면_타임아웃후_None을_돌려준다(monkeypatch, slow_server):
    """★ 6-A 핵심 — 그전까지 어느 VLM 호출에도 타임아웃이 없었다. 폐쇄망
    DNS 블랙홀·사내 서버 다운이 탐지 스레드를 통째로 묶으면 안 된다."""
    monkeypatch.setenv("TOT_VLM_BACKEND", "openai_compatible")
    monkeypatch.setenv("TOT_VLM_BASE_URL", slow_server)
    monkeypatch.setenv("TOT_VLM_MODEL", "test-model")

    start = time.monotonic()
    result = VLM.call_vlm("sys", "user", None, timeout_sec=1.0)
    elapsed = time.monotonic() - start

    assert result is None
    # 서버가 20초 뒤에야 응답하는데 훨씬 일찍 포기했는지 — 타임아웃이
    # 실제로 걸렸는지의 직접 증거다. 문턱을 넉넉히 둔 이유 — openai SDK
    # 자체 오버헤드가 순수 httpx2보다 수 배 크다는 것을 실측으로 확인했다
    # (같은 서버·문턱에서 httpx2 단독 0.75s, openai SDK 경유 최대 5초대
    # 관측). 그래도 서버의 20초에는 한참 못 미쳐야 "타임아웃이 실제로
    # 걸렸다"는 증거로 유효하다.
    assert elapsed < 10.0, f"타임아웃이 걸리지 않고 서버 응답을 다 기다렸다({elapsed:.2f}s)"


# --- 순수 유틸리티 --------------------------------------------------------------


def test_json_코드펜스를_걷어낸다():
    raw = '```json\n{"a": 1}\n```'
    assert VLM.extract_json(raw) == '{"a": 1}'


def test_잡담이_섞여도_중괄호_구간만_남긴다():
    raw = '물론입니다! 아래가 결과입니다:\n{"a": 1}\n이상입니다.'
    assert VLM.extract_json(raw) == '{"a": 1}'


def test_이미지가_없으면_None():
    assert VLM.to_jpeg_bytes(None) is None


def test_이미_bytes면_그대로_통과():
    assert VLM.to_jpeg_bytes(b"\xff\xd8\xff") == b"\xff\xd8\xff"
