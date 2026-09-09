"""화면이 **바깥에서 아무것도 받아오지 않는가**.

납품 대상이 폐쇄망(망분리)이다. 화면이 인터넷에서 스크립트·글꼴·스타일을
내려받도록 두면 **관제실에서 그대로 깨진다.**

경남 제안요청서 **PER-009** — 「시스템 운영 시 **외부 인터넷 연결이 필수적으로
요구되지 않는 구조**를 제공하여야 한다」

## 왜 시험으로 막는가

2026-08-19 이전에 `index.html` 이 hls.js 를 `cdn.jsdelivr.net` 에서 받아
왔다. 개발 PC 에서는 인터넷이 되니 **멀쩡해 보이고, 관제실에 넣는 순간**
드러난다 — 사람 눈으로는 안 걸리는 종류다.

더 나쁜 것은 그 `<script>` 가 순서대로 실행되는 방식이라, **실패를 기다리는
동안 뒤따르는 app.js 가 멈춰** 화면 전체가 비어 있었다는 점이다.

⚠️ **이 시험은 「지금 깨끗한가」가 아니라 「다시 더러워지지 않는가」를 본다.**
새 화면을 만들 때 CDN 한 줄을 붙이는 것은 너무 쉽다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / \
    "src" / "tot_dashboard" / "service" / "templates"
STATIC = TEMPLATES.parent / "static"
VENDOR = STATIC / "vendor"

# 화면이 실제로 **받아오는** 자리만 본다. 주석·문구에 적힌 주소(출처 표기,
# 설명용 링크)까지 막으면 문서를 못 쓰게 된다.
_FETCHING = re.compile(
    r"""(?:src|href)\s*=\s*["'](?P<url>(?:https?:)?//[^"']+)["']""",
    re.IGNORECASE)

# 사람이 눌러서 새 창으로 가는 링크는 허용한다 — 그건 화면이 받아오는 게
# 아니라 사용자가 브라우저로 여는 것이다. 지도 타일 주소는 설정값이라
# 템플릿에 하드코딩돼 있지 않다.
_ALLOWED_TAGS = ("<a ",)


def _html_files() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def _external_refs(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(t in stripped for t in _ALLOWED_TAGS):
            continue
        for m in _FETCHING.finditer(line):
            out.append(m.group("url"))
    return out


@pytest.mark.parametrize("path", _html_files(), ids=lambda p: p.name)
def test_템플릿이_바깥에서_받아오지_않는다(path: Path):
    """★ 폐쇄망에서 깨지는 유일한 원인을 막는다."""
    refs = _external_refs(path.read_text(encoding="utf-8"))
    assert not refs, (
        f"{path.name} 이 바깥 주소에서 받아옵니다: {refs}\n"
        "폐쇄망 관제실에서 깨집니다. static/vendor/ 에 넣고 "
        "/static/... 경로로 부르세요 (경남 제안요청서 PER-009).")


# ⚠️ **XML 이름공간은 주소가 아니다.** `http://www.w3.org/2000/svg` 는
# SVG 요소를 만들 때 쓰는 식별자일 뿐 브라우저가 받아오지 않는다. 이걸 막으면
# SVG 를 못 그린다 — 실제로 이 시험이 처음에 app.js 를 잘못 잡았다.
_XML_NAMESPACES = ("www.w3.org/2000/svg", "www.w3.org/1999/xlink",
                   "www.w3.org/1999/xhtml", "www.w3.org/XML/1998/namespace")


def test_js_가_바깥에서_받아오지_않는다():
    """스크립트가 실행 중에 바깥을 부르면 화면은 멀쩡한데 기능만 죽는다."""
    bad = []
    for js in sorted(STATIC.rglob("*.js")):
        # 반입한 라이브러리는 우리가 고치지 않는다 — 안에 무슨 주소가 있든
        # 우리 코드가 그걸 부르지 않으면 폐쇄망에서 문제가 없다.
        if VENDOR in js.parents:
            continue
        for line in js.read_text(encoding="utf-8", errors="replace").splitlines():
            if not re.search(r"""["'`](?:https?:)?//(?!localhost|127\.)""", line):
                continue
            # 주석은 뺀다 — 출처를 적어 두는 것까지 막을 이유가 없다.
            if line.strip().startswith(("//", "*", "/*")):
                continue
            if any(ns in line for ns in _XML_NAMESPACES):
                continue
            bad.append(f"{js.name}: {line.strip()[:90]}")
    assert not bad, "스크립트가 바깥 주소를 부릅니다:\n" + "\n".join(bad)


# --- 반입 라이브러리 -------------------------------------------------------


def test_hls_js_가_저장소에_있다():
    """⚠️ 파일이 없으면 실시간 영상이 Chrome·Edge 에서 안 나온다."""
    f = VENDOR / "hls.min.js"
    assert f.exists(), "static/vendor/hls.min.js 가 없습니다."
    # 최소화 파일이라 크기로만 온전함을 본다. 빈 파일·오류 페이지가
    # 저장돼 있으면 화면에서는 조용히 실패한다.
    assert f.stat().st_size > 100_000, "hls.min.js 가 손상된 것 같습니다."


def test_반입_라이브러리에_라이선스가_함께_있다():
    """⚠️ Apache-2.0 은 재배포 시 라이선스 사본을 함께 두도록 요구한다.

    최소화된 hls.min.js 안에는 **라이선스 표기가 없다.** 사본을 지우면
    라이선스 위반이 된다.
    """
    lic = VENDOR / "hls.js-LICENSE.txt"
    assert lic.exists(), "hls.js-LICENSE.txt 가 없습니다 — 지우면 안 됩니다."
    text = lic.read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Dailymotion" in text


def test_반입_경위가_문서로_남아_있다():
    """어디서 왔는지 모르는 파일이 저장소에 있으면 안 된다."""
    doc = VENDOR / "README.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "hls.js" in text and "Apache-2.0" in text
    # 무결성 값이 적혀 있어야 판올림 때 바뀐 것을 알아볼 수 있다.
    assert re.search(r"sha-?256", text, re.IGNORECASE)


def test_index_가_반입본을_쓴다():
    """★ 되돌아가지 않았는지 직접 확인한다.

    ⚠️ **주석에 적힌 옛 주소까지 막지 않는다.** 「왜 바꿨는지」를 남긴 설명이
    시험을 깨면, 다음 사람은 설명을 지우는 쪽을 고른다. 실제로 **받아오는
    자리**만 본다.
    """
    text = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/hls.min.js" in text
    assert not _external_refs(text)
