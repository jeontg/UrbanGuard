"""flood-service(``service/flood_service.py``) — 인자 없는 GET API가
터지지 않는가.

⚠️ 2026-08-31 신설 — API 게이트웨이 Phase 4로 침수 API가
platform-shell(``service/main.py``)에서 이 별도 서비스로 옮겨지면서,
``tests/service/test_api_smoke.py``가 더 이상 이 경로들을 검사하지
않게 됐다. 같은 안전망을 이 서비스 자체에도 그대로 만든다(패턴은
test_road_service_api_smoke.py와 동일).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.flood_service import app

# ⚠️ SSE 스트림은 응답이 끝나지 않는다 — 이 시험이 그걸 그대로 GET하면
# 멈춘다(test_api_smoke.py의 같은 이유로 둔 _SKIP 참고).
_SKIP = {"/api/stream/flood-risk"}


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
def client(seeded_users, login_cross_service):
    c = TestClient(app)
    login_cross_service(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


@pytest.mark.parametrize("path", _get_apis())
def test_api_가_터지지_않는다(client, db_schema, path):
    r = client.get(path)
    assert r.status_code < 500, f"{path} → {r.status_code}\n{r.text[:300]}"


def test_검사_대상을_실제로_찾았다():
    """목록이 비면 위 시험이 아무것도 안 하고 통과한다."""
    assert len(_get_apis()) >= 2
