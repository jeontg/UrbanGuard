"""배경 테마가 바뀌어도 시인성이 유지되는지 (2026-08-27 신고 시정).

**신고받은 증상 (같은 날, 두 번째)**:
1. 어두운 배경에서 CCTV 관리의 「탐지 지정」·「보정」·「수정」 팝업 테두리가
   구분되지 않는다.
2. 관제 화면 배경(S-85)을 밝은 색으로 바꾸면 어딘가 글자가 안 보인다.

**조사 결과**: ``static/styles.css``의 상황판(``.ug-board``)·모달·노면
실시간(``.rl-*``) 구역이 flood3 원본을 옮겨온 뒤로 GitHub Dark 팔레트
(``#8b949e``·``#161b22``·``#0d1117``·``#21262d``·``#30363d``·``#c9d1d9``·
``#e6edf3``)를 하드코딩해 왔다. **가장 심각했던 패턴**은 상자 배경은
고정(``#0d1117`` 등)인데 그 안 글자는 이미 ``var(--text)``/``var(--muted)``
로 테마를 따르던 경우다(``.rl-card``·``.roi-note code``·``.road-select``
등) — 배경이 밝은 테마로 바뀌면 글자만 따라 밝아져(원래 어두운 배경에
맞춰 밝은 값이던 게 이제 밝은 배경에 맞춰 어두운 값으로) 두 조합이
어긋날 수 있었다.

지켜야 할 것.

* **팝오버·모달 상자는 ``--border-strong``(패널과의 밝기 차이가 일반
  ``--border``보다 뚜렷이 큰 값)을 쓴다** — 배경이 무엇이든 확실히
  갈라져 보여야 한다
* **구조적인 배경·글자색(상황판 카드·모달·노면 카드 등)은 테마 변수를
  쓴다** — 관리자가 배경을 바꾸면 함께 움직여야 한다
* **등급·추세처럼 의미가 있는 색(빨강=심각·주황=경고·초록=양호)은
  고정값으로 남는다** — 어느 테마에서도 그 자체로 뜻이 통해야 한다.
  이 시험은 그런 의미색까지 변수로 바꾸라고 요구하지 않는다
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tot_dashboard.core import settings as S
from tot_dashboard.service import main as _m


@pytest.fixture(scope="module")
def css_text() -> str:
    path = Path(_m.__file__).parent / "static" / "styles.css"
    return path.read_text(encoding="utf-8")


# --- 회귀 방지: 신고 계기가 됐던 정확한 조합들이 되살아나지 않는지 -----------

@pytest.mark.parametrize("selector_hint, banned", [
    (".rl-card 배경",       ".rl-card { border: 1px solid var(--border); border-radius: 8px; background: #0d1117;"),
    (".road-select 배경",   ".road-select { background: #0d1117;"),
    (".roi-note code 배경", ".roi-note code { background: #0d1117;"),
    (".ug-board .rec 배경", ".ug-board .rec { background: #1c2330;"),
    (".modalbox 배경",      ".modalbox { background: #0d1117;"),
    (".ug-board .card 배경", ".ug-board .card { border: 1px solid #30363d; border-radius: 10px; background: #161b22;"),
])
def test_신고됐던_고정_배경_조합이_되살아나지_않는다(css_text, selector_hint, banned):
    assert banned not in css_text, (
        f"{selector_hint} 규칙이 예전의 고정 다크 배경으로 되돌아갔습니다 — "
        "이 안에 var(--text)/var(--muted)로 테마를 따르는 글자가 있으면 "
        "밝은 테마에서 다시 안 보이게 됩니다.")


def test_팝오버_모달_전용_강한_경계선_클래스가_존재한다(css_text):
    assert ".ug-popover-panel" in css_text
    assert "border-strong" in css_text


def test_modalbox도_강한_경계선을_쓴다(css_text):
    assert ".modalbox { background: var(--panel); border: 1.5px solid var(--border-strong)" in css_text


# --- derive_theme() 이 만드는 실제 값으로 밝기 차이를 확인 -------------------

@pytest.mark.parametrize("bg", [
    "#0F1420",  # 기본값
    "#000000",  # 블랙 프리셋
    "#F5F7FA",  # 관리자가 고를 법한 밝은 회백색
    "#FFFFFF",  # 순백
    "#12161C", "#0B1A2A", "#141018",  # 나머지 프리셋
])
def test_어떤_배경을_고르든_팝업_테두리가_패널과_뚜렷이_갈린다(bg):
    """★ 핵심 회귀 시험 — S-85에서 고를 수 있는 프리셋 전부와, 관리자가
    자유 입력으로 밝은 회백색을 고른 경우까지 훑는다."""
    theme = S.derive_theme(bg)
    panel_l = S.luminance(S.parse_hex(theme["panel"]))
    strong_l = S.luminance(S.parse_hex(theme["border_strong"]))
    # 사람 눈에 확실히 갈리려면 밝기 차이가 최소한 이 정도는 나야 한다는
    # 느슨한 하한선이다 — 정확한 WCAG 비대비 공식이 아니라, 이전 값
    # (--border, 배경과 18단계 차이)보다 명백히 커야 한다는 취지의 안전망.
    assert abs(strong_l - panel_l) > 0.02


@pytest.mark.parametrize("bg", ["#0F1420", "#FFFFFF", "#F5F7FA", "#000000"])
def test_본문_글자와_배경의_대비_경고가_여전히_동작한다(bg):
    """이번 작업이 기존 대비 검사(S-85, 9-3절)를 깨지 않았는지 확인한다."""
    # 프리셋/기본값은 전부 대비 경고가 없어야 한다(기존 계약).
    assert S.contrast_warning(bg) is None
