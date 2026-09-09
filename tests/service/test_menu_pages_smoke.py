"""메뉴에 걸린 화면이 **실제로 열리는가** (2026-08-19 전체 점검).

## 왜 이 시험이 있나

전체 점검에서 **메뉴 28건 중 10건을 시험이 한 번도 열지 않는다**는 것을
찾았다. 그중 `/settings/model` 은 **2026-08-16 에 실제로 터졌던 화면**이다 —
오류 표에 ``UndefinedError: 'road_gap' is undefined`` 가 남아 있다.

★ **템플릿 변수 하나가 빠지면 화면이 통째로 500 이 된다.** 그런데 그것을
잡을 시험이 없었다. 같은 날 ``'weather' is undefined`` 31회,
``'card_orders' is undefined`` 16회도 같은 종류였다.

⚠️ **이 시험은 화면 내용을 검사하지 않는다.** 「열리는가」만 본다. 얕지만,
**터지는 것은 확실히 잡는다** — 지금까지 그것조차 못 잡고 있었다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.main import app

_AUTH = Path(__file__).resolve().parents[2] / "src" / "tot_dashboard" / "core" / "auth.py"


def _menu_hrefs() -> list[str]:
    """메뉴에 실제로 걸린 주소. 목록을 손으로 적지 않는다 —
    새 화면이 늘면 **시험이 저절로 따라와야** 한다."""
    text = _AUTH.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'"href":\s*"([^"]+)"', text)))


@pytest.fixture(scope="module")
def client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


@pytest.mark.parametrize("href", _menu_hrefs())
def test_메뉴_화면이_열린다(client, db_schema, href):
    r = client.get(href)
    # 403 은 권한 설계상 정상일 수 있다(관리자에게도 안 열리는 화면).
    # ⚠️ 500 은 어떤 이유로도 정상이 아니다 — 템플릿 변수 누락이 여기서 잡힌다.
    assert r.status_code < 500, (
        f"{href} 가 {r.status_code} 로 열리지 않습니다.\n"
        f"{r.text[:400]}")


def test_메뉴_주소를_실제로_찾았다():
    """★ 정규식이 0건을 돌려주면 위 시험이 **아무것도 안 하고 통과**한다."""
    assert len(_menu_hrefs()) >= 20
