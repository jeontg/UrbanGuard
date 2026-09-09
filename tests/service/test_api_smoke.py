"""인자 없는 GET API 가 **터지지 않는가** (2026-08-19 전체 점검).

## 왜 이 시험이 있나

전체 점검에서 **API 36건 중 10건이 시험에 한 번도 나오지 않는다**는 것을
찾았다. 화면과 같은 종류의 구멍이다 — 응답 형태가 바뀌거나 컨텍스트가
빠져도 **아무도 모른다.**

## ⚠️ 이 시험이 하지 않는 것 (범위를 밝힌다)

* **POST 는 빼놓았다** — 부작용이 있다. 분석을 실제로 돌리거나 설정을 바꾼다
* **경로 변수가 필요한 것은 빼놓았다** — 유효한 id 를 만들어야 하고, 그건
  각 기능의 시험이 할 일이다
* **응답 내용을 검사하지 않는다** — 「터지지 않는가」만 본다

★ 얕지만 **터지는 것은 확실히 잡는다.** 지금까지 그것조차 못 잡고 있었다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.main import app

# ⚠️ 부르면 **상태가 진행되는** API 는 뺀다.
#    `/api/crowd/live` 는 분석기의 step() 을 돌려 시간축을 진행시킨다 —
#    연막 시험이 그것을 건드리면 다른 시험의 전제가 흔들린다.
_SKIP = {"/api/crowd/live", "/api/stream/risk"}


def _get_apis() -> list[str]:
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", set()) or set()
        if not path.startswith("/api/") or "{" in path:
            continue
        if "GET" not in methods or path in _SKIP:
            continue
        out.append(path)
    return sorted(set(out))


@pytest.fixture(scope="module")
def client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


@pytest.mark.parametrize("path", _get_apis())
def test_api_가_터지지_않는다(client, db_schema, path):
    r = client.get(path)
    # 403 은 권한 설계상 정상일 수 있다. 500 은 어떤 이유로도 정상이 아니다.
    assert r.status_code < 500, f"{path} → {r.status_code}\n{r.text[:300]}"


def test_검사_대상을_실제로_찾았다():
    """★ 목록이 비면 위 시험이 **아무것도 안 하고 통과**한다.

    ⚠️ 2026-08-31 — API 게이트웨이 Phase 1~4로 인파관리(``/api/crowd/*``·
    ``/api/cases*``)·노면관리(``/api/road/*`` 등)·침수(``/api/flood-
    risk*``)·교통위험(``/api/risk*``)이 전부 별도 서비스로 옮겨지면서
    이 프로세스(main.app)의 GET /api/* 목록이 계속 줄었다(각 서비스는
    자기만의 스모크 시험을 따로 가진다 —
    ``test_crowd_service_api_smoke.py``·``test_road_service_api_smoke.py``·
    ``test_flood_service_api_smoke.py``·
    ``test_traffic_service_api_smoke.py`` 참고). 문턱값을 이관 후 실측
    (5개)과 같게 잡는다 — 이 시험의 목적은 "정확히 몇 개인가"가 아니라
    "목록 추출 로직 자체가 조용히 깨져 빈 목록을 통과시키지 않는가"다.
    """
    assert len(_get_apis()) >= 5
