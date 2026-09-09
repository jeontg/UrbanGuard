"""기관 로고 교체 (core/branding.py).

파일을 실제로 쓰므로 저장 위치를 임시 폴더로 갈아끼운다.
"""
from __future__ import annotations

import io

import pytest

from tot_dashboard.core import branding


def _png_bytes(w: int, h: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def logo_dir(tmp_path, monkeypatch):
    d = tmp_path / "branding"
    monkeypatch.setattr(branding, "LOGO_DIR", d)
    return d


def test_업로드_전에는_기본_로고를_쓴다():
    assert branding.logo_url("", "dark", "ui").endswith("urbanguard_logo_ui_dark.png")
    assert branding.logo_url("", "light", "tight").endswith(
        "urbanguard_logo_tight_light.png")


def test_로고를_올리면_그_주소를_쓴다(logo_dir):
    name, err = branding.save_logo(_png_bytes(400, 90), "our_logo.png")
    assert err is None
    assert branding.logo_url(name) == f"/branding/logo/{name}"


def test_크기를_00px_00px_형태로_표기한다(logo_dir):
    name, err = branding.save_logo(_png_bytes(408, 88), "logo.png")
    assert err is None
    assert branding.format_size(branding.image_size(name)) == "(408px, 88px)"


def test_크기를_읽지_못하면_표기로_알린다():
    assert branding.format_size(None) == "(크기 확인 불가)"


def test_SVG는_받지_않는다(logo_dir):
    """스크립트를 품을 수 있어 형식 자체를 막는다."""
    name, err = branding.save_logo(b"<svg/>", "logo.svg")
    assert name == ""
    assert "SVG" in err


def test_확장자만_PNG인_가짜_파일을_거른다(logo_dir):
    name, err = branding.save_logo(b"not an image at all", "logo.png")
    assert name == ""
    assert "이미지" in err


def test_너무_큰_파일을_거른다(logo_dir, monkeypatch):
    monkeypatch.setattr(branding, "MAX_BYTES", 100)
    name, err = branding.save_logo(_png_bytes(400, 90), "logo.png")
    assert name == ""
    assert "너무 큽니다" in err


def test_한_변이_너무_긴_이미지를_거른다(logo_dir, monkeypatch):
    monkeypatch.setattr(branding, "MAX_EDGE", 50)
    name, err = branding.save_logo(_png_bytes(400, 20), "logo.png")
    assert name == ""
    assert "너무 큽니다" in err


def test_새로_올리면_이전_로고를_남기지_않는다(logo_dir):
    """쌓아 두면 어느 것이 현재 로고인지 알 수 없다."""
    branding.save_logo(_png_bytes(400, 90), "first.png")
    branding.save_logo(_png_bytes(300, 70), "second.jpg")
    assert sorted(p.name for p in logo_dir.glob("logo.*")) == ["logo.jpg"]


def test_경로_조작을_막는다(logo_dir):
    """`../` 가 섞인 이름이 와도 저장 폴더 밖을 가리키지 않는다."""
    p = branding.logo_path("../../etc/passwd")
    assert p.parent == logo_dir


def test_되돌리면_파일이_사라지고_기본_로고로_간다(logo_dir):
    name, _ = branding.save_logo(_png_bytes(400, 90), "logo.png")
    branding.clear_logo(name)
    assert not branding.logo_path(name).exists()
    assert branding.logo_url(name).startswith("/static/brand/")
