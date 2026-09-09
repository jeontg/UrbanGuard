"""화면 설정(S-85) — 색 파생과 접근성 대비 검사.

배경색 하나만 바꾸고 글자색을 그대로 두면 대비가 무너진다. 그 안전장치가
실제로 동작하는지 확인한다(설계서 9-3절).
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import settings as S


# --- 색 파싱 -----------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("#0F1420", (15, 20, 32)),
    ("0F1420", (15, 20, 32)),
    ("#abc", (170, 187, 204)),
    ("#FFFFFF", (255, 255, 255)),
])
def test_parse_hex_accepts_common_forms(raw, expected):
    assert S.parse_hex(raw) == expected


@pytest.mark.parametrize("raw", ["", "#12345", "nonsense", "#GGGGGG", None])
def test_parse_hex_rejects_bad_input(raw):
    assert S.parse_hex(raw) is None


# --- 색 파생 -----------------------------------------------------------------
def test_dark_background_gets_light_text():
    theme = S.derive_theme("#0F1420")
    assert theme["text"] == "#E7ECF5"


def test_light_background_flips_text_to_dark():
    """밝은 배경을 고르면 글자색이 자동으로 어두워져야 한다.

    이게 없으면 흰 배경에 흰 글자가 되어 화면이 통째로 안 보인다.
    """
    theme = S.derive_theme("#F5F7FA")
    assert theme["text"] == "#161E2E"


def test_logo_variant_follows_background_brightness():
    """로고도 글자색과 같은 규칙으로 뒤집혀야 한다.

    다크판 로고의 "Urban"은 흰 글자라, 밝은 배경에 다크판을 쓰면 글자가
    통째로 사라진다(실제로 발생했던 결함).
    """
    assert S.derive_theme("#0F1420")["logo"] == "dark"
    assert S.derive_theme("#FFFFFF")["logo"] == "light"
    assert S.derive_theme("#F5F7FA")["logo"] == "light"


def test_logo_variant_matches_text_color_rule():
    """글자색과 로고 판단 기준이 어긋나면 안 된다."""
    for bg in ("#000000", "#0F1420", "#808080", "#F5F7FA", "#FFFFFF"):
        t = S.derive_theme(bg)
        expect = "light" if t["text"] == "#161E2E" else "dark"
        assert t["logo"] == expect, bg


def test_panel_and_border_derive_from_background():
    theme = S.derive_theme("#0F1420")
    # 어두운 배경에서는 패널·경계선이 배경보다 밝아야 구분된다
    assert S.luminance(S.parse_hex(theme["panel"])) > S.luminance(S.parse_hex(theme["bg"]))
    assert S.luminance(S.parse_hex(theme["border"])) > S.luminance(S.parse_hex(theme["panel"]))


def test_derive_theme_falls_back_on_garbage():
    """잘못된 값이 들어와도 화면은 떠야 한다."""
    theme = S.derive_theme("not-a-color")
    assert theme["bg"] == "#0F1420"


# --- 팝업/모달용 강한 경계선 (2026-08-27, CCTV 관리 팝오버 신고) -------------
@pytest.mark.parametrize("bg", ["#000000", "#0F1420", "#808080", "#F5F7FA", "#FFFFFF"])
def test_팝업_경계선은_일반_경계선보다_배경에서_더_멀리_떨어진다(bg):
    """★ 회귀 방지 핵심 — ``border``(패널과 18단계 차이)만으로는 팝업이
    비슷한 톤의 화면 위에서 묻힌다는 신고가 실제 계기였다. ``border_strong``
    은 어떤 배경(아주 어둡든 아주 밝든)에서도 패널과의 밝기 차이가 항상
    ``border``보다 커야 한다."""
    theme = S.derive_theme(bg)
    bg_l = S.luminance(S.parse_hex(theme["bg"]))
    panel_l = S.luminance(S.parse_hex(theme["panel"]))
    border_l = S.luminance(S.parse_hex(theme["border"]))
    strong_l = S.luminance(S.parse_hex(theme["border_strong"]))
    # 어두운 배경이면 밝을수록, 밝은 배경이면 어두울수록 "더 간다".
    if bg_l < 0.5:
        assert strong_l > border_l >= panel_l
    else:
        assert strong_l < border_l <= panel_l
    # 패널과의 밝기 차이가 일반 경계선보다 뚜렷하게 커야 한다(구분 목적).
    assert abs(strong_l - panel_l) > abs(border_l - panel_l)


# --- 대비 검사 ---------------------------------------------------------------
def test_no_warning_for_default_background():
    assert S.contrast_warning("#0F1420") is None


def test_no_warning_for_light_background_because_text_flips():
    assert S.contrast_warning("#F5F7FA") is None


def test_warns_on_mid_tone_where_neither_text_color_works():
    """중간 밝기는 흑·백 어느 글자색으로도 4.5:1을 못 넘긴다."""
    warn = S.contrast_warning("#808080")
    assert warn is not None and "4.5:1" in warn


def test_warns_on_invalid_color():
    assert "형식" in (S.contrast_warning("zzz") or "")


def test_contrast_ratio_extremes():
    white, black = (255, 255, 255), (0, 0, 0)
    assert S.contrast_ratio(white, black) == pytest.approx(21.0, abs=0.05)
    assert S.contrast_ratio(white, white) == pytest.approx(1.0, abs=0.001)


# --- 프리셋 ------------------------------------------------------------------
def test_every_preset_passes_the_contrast_check():
    """미리 준비한 배경은 전부 접근성 기준을 통과해야 한다.

    프리셋에 경고가 뜨는 색을 넣어 두면 「권장값인데 경고가 뜬다」는
    모순이 생긴다.
    """
    for hex_, name, _desc in S.PRESETS:
        assert S.contrast_warning(hex_) is None, f"{name}({hex_})"
