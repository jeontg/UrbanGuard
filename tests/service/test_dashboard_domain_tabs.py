"""상황판 탭 분리 — 침수와 교통이 별도 탭으로 그려진다
(flood/traffic 도메인 분리 7단계, 2026-08-21).

## 왜 이 시험이 있나

예전에는 탭 하나(「침수·교통위험」)에 두 판정이 **한 카드의 위·아래**로 붙어
있었다. 관제요원이 「이게 침수 때문인지 정체 때문인지」 구분할 수 없었다.

⚠️ **문자열 포함 검사만으로는 부족하다.** 지난 회차(S-88)에서 팝업 마크업이
통째로 `{% block title %}` 안에 들어가 **DOM 에 존재하지 않는데도** 문자열
검사는 통과한 적이 있다. 그래서 여기서는 「어느 섹션 안에 있는가」까지 본다.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client


@pytest.fixture(scope="module")
def page(client) -> str:
    r = client.get("/flood")
    assert r.status_code == 200
    return r.text


def _body_after_head(html: str) -> str:
    """``<head>`` 를 잘라낸 나머지 — ``<title>`` 안에 갇힌 마크업을 「있다」로
    세지 않기 위해서다(S-88 에서 실제로 그런 결함이 있었다)."""
    i = html.find("</head>")
    return html[i:] if i >= 0 else html


def test_교통과_침수_탭이_각각_있다(page):
    body = _body_after_head(page)
    assert 'data-tab="traffic"' in body
    assert 'data-tab="flood"' in body


def test_두_탭_섹션이_실제_DOM에_있다(page):
    body = _body_after_head(page)
    assert 'id="tab-traffic"' in body
    assert 'id="tab-flood"' in body


def test_두_그리드가_서로_다른_섹션_안에_있다(page):
    """★ 구조 검사 — 문자열이 있는 것만으로는 같은 카드에 겹쳐 있던 예전
    상태와 구분되지 않는다. 각 그리드가 자기 섹션 안에 있는지 본다."""
    body = _body_after_head(page)
    traffic = re.search(r'id="tab-traffic".*?</section>', body, re.S)
    flood = re.search(r'id="tab-flood".*?</section>', body, re.S)
    assert traffic and flood, "두 탭 섹션을 찾지 못했다"
    assert 'id="traffic-grid"' in traffic.group(0)
    assert 'id="flood-grid"' in flood.group(0)
    # 서로의 그리드를 품고 있으면 분리가 안 된 것이다.
    assert 'id="flood-grid"' not in traffic.group(0)
    assert 'id="traffic-grid"' not in flood.group(0)


def test_두_탭이_상황판_스타일_스코프를_공유한다(page):
    """CSS 스코프가 `#tab-flood` 에서 `.ug-board` 로 바뀌었다 — 클래스를
    빠뜨리면 카드가 스타일 없이 맨몸으로 뜬다(눈으로만 보이는 종류의 결함)."""
    body = _body_after_head(page)
    for sec in ("tab-traffic", "tab-flood"):
        m = re.search(rf'<section id="{sec}"[^>]*>', body)
        assert m, f"{sec} 섹션 태그를 찾지 못했다"
        assert "ug-board" in m.group(0), f"{sec} 에 ug-board 클래스가 없다"


def test_옛_통합_탭_라벨이_남아_있지_않다(page):
    """「침수·교통위험」이 화면에 남아 있으면 나눠 놓고 이름만 옛것이다."""
    assert "침수·교통위험" not in _body_after_head(page)


def test_카드_그리기_함수가_둘로_나뉘어_있다():
    """``card()`` 하나가 두 도메인을 그리던 것을 나눴다. 정적 자산이라
    렌더링 결과가 아니라 파일을 본다."""
    from tot_dashboard.common.config import PROJECT_ROOT

    js = (PROJECT_ROOT / "src" / "tot_dashboard" / "service" / "static"
          / "app.js").read_text(encoding="utf-8")
    assert "function trafficCard(" in js
    assert "function floodCard(" in js
    # 교통 카드가 침수 섹션을 다시 품으면 분리가 무의미하다.
    tc = js[js.index("function trafficCard("):]
    tc = tc[:tc.index("\nfunction ")]
    assert "floodSection(" not in tc
