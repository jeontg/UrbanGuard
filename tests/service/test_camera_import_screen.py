"""S-80 자동 수집 — 지역 프리셋과 최소 해상도 선택이 화면에 나오는가.

지켜야 할 것.

* **전국 시도를 고를 수 있다** — 부울경만 되면 타 지역 제안이 막힌다
* **해상도로 거를 수 있다** — 저해상도 지점은 탐지 품질을 떨어뜨린다
* **0건일 때 왜 0건인지 알려준다** — 해상도 조건 탓인지 지역 탓인지
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.core import cctv_sources as SRC
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    # 수명주기를 켜지 않는다 — runner 는 프로세스당 한 번만 start 할 수 있다.
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_S80_화면에_전국_시도가_모두_나온다(client):
    html = client.get("/settings/cameras").text
    for label in ("부산광역시", "서울특별시", "제주특별자치도",
                  "강원특별자치도", "전북특별자치도", "세종특별자치시"):
        assert label in html, label


def test_S80_화면에_최소_해상도_선택이_있다(client):
    html = client.get("/settings/cameras").text
    assert 'name="min_height"' in html
    assert "HD(720p) 이상" in html
    assert "FHD(1080p) 이상" in html
    assert "제한 없음" in html


def test_수집이_0건이면_해상도_조건을_짚어_준다(client, monkeypatch):
    """0건일 때 「없다」로만 끝나면 조건을 낮춰 볼 생각을 못 한다."""
    monkeypatch.setattr(SRC, "api_key", lambda: "테스트키")
    monkeypatch.setattr(SRC, "_request", lambda params: {"response": {"data": []}})

    r = client.post("/settings/cameras/import/fetch",
                    data={"region": "jeju", "min_height": "1080"})
    assert r.status_code == 400
    assert "제주특별자치도" in r.text
    assert "1080p" in r.text
    assert "제한 없음" in r.text


def test_해상도_조건이_없으면_그_안내는_안_나온다(client, monkeypatch):
    monkeypatch.setattr(SRC, "api_key", lambda: "테스트키")
    monkeypatch.setattr(SRC, "_request", lambda params: {"response": {"data": []}})

    r = client.post("/settings/cameras/import/fetch",
                    data={"region": "seoul", "min_height": "0"})
    assert r.status_code == 400
    assert "서울특별시" in r.text
    assert "최소 해상도를" not in r.text
