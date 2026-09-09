"""S-01 배경지도 — 설정과 화면 전환.

지켜야 할 것.

* **기본은 꺼짐** — 망분리 환경에서 켜 두면 매번 실패한다
* **비우면 끄는 것** — 빈 값이 오류가 되면 되돌릴 수 없다
* **주소 형식을 저장 전에 검사한다** — 틀린 주소는 화면에서 조용히 실패한다
* **켜져 있어도 배치 도식은 화면에 남는다** — 타일이 안 오면 되돌아가야 한다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.service.main import app

OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def _restore(db_schema):
    """시험이 바꾼 설정을 되돌린다. 다음 시험이 지도가 켜진 상태를 보면 안 된다."""
    yield
    with get_session() as db:
        S.set_value(db, S.KEY_MAP_TILE_URL, "")
        S.set_value(db, S.KEY_MAP_ATTRIBUTION, "")
        S.set_value(db, S.KEY_MAP_MAX_ZOOM, "18")
        db.commit()
    S.invalidate()


def _save(client, **over):
    data = {"org_name": "테스트시", "solution_name": "통합 도시안전 관제",
            "board_bg": "#0F1420", "map_tile_url": "", "map_attribution": "",
            "map_max_zoom": "18"}
    data.update(over)
    return client.post("/settings/org", data=data)


# ---------- 기본값 ----------

def test_기본은_꺼져_있다():
    assert S.DEFAULTS[S.KEY_MAP_TILE_URL] == ""


def test_꺼져_있으면_상황판이_배치_도식을_쓴다(client):
    html = client.get("/").text
    assert "ug-schematic" in html
    assert "ugmap.js" not in html            # 스크립트를 아예 안 받는다
    assert "타일 주소를 넣으세요" in html


# ---------- 저장 검증 ----------

def test_주소를_저장하면_상황판이_지도를_쓴다(client):
    _save(client, map_tile_url=OSM, map_attribution="© OpenStreetMap 기여자")
    html = client.get("/").text
    assert "ugmap.js" in html
    assert "ug-basemap" in html
    assert "OpenStreetMap" in html


def test_켜져_있어도_배치_도식은_화면에_남는다(client):
    """타일이 안 오면 되돌아갈 자리가 있어야 한다."""
    _save(client, map_tile_url=OSM)
    html = client.get("/").text
    assert "ug-schematic" in html
    assert "hidden" in html


def test_비우면_다시_꺼진다(client):
    _save(client, map_tile_url=OSM)
    _save(client, map_tile_url="")
    html = client.get("/").text
    assert "ugmap.js" not in html


@pytest.mark.parametrize("bad,why", [
    ("tile.example.org/{z}/{x}/{y}.png", "http"),
    ("https://tile.example.org/{x}/{y}.png", "{z}"),
    ("https://tile.example.org/{z}/{y}.png", "{x}"),
    ("https://tile.example.org/tile.png", "{z}"),
])
def test_틀린_주소는_저장되지_않는다(client, bad, why):
    r = _save(client, map_tile_url=bad)
    assert r.status_code == 400
    assert why in r.text
    with get_session() as db:
        assert S.get(S.KEY_MAP_TILE_URL, db) == ""


@pytest.mark.parametrize("zoom", ["4", "23", "숫자아님"])
def test_확대_단계_범위를_지킨다(client, zoom):
    r = _save(client, map_tile_url=OSM, map_max_zoom=zoom)
    assert r.status_code == 400


def test_확대_단계가_저장된다(client):
    _save(client, map_tile_url=OSM, map_max_zoom="15")
    with get_session() as db:
        assert S.get(S.KEY_MAP_MAX_ZOOM, db) == "15"


# ---------- 지도에 필요한 자료 ----------

def test_지점에_위경도가_실려_있다(client):
    """배치 도식은 백분율로 찍지만 배경지도는 실제 좌표가 필요하다."""
    from tot_dashboard.service import board_map
    from tot_dashboard.service.main import BLOCKS

    pts = board_map.build_points(BLOCKS)
    assert pts, "감시 지점이 없어 확인할 수 없다"
    for p in pts:
        assert isinstance(p["lat"], float)
        assert isinstance(p["lng"], float)
        assert "x" in p and "y" in p        # 도식용 좌표도 그대로 있다


def test_지도_스크립트가_배포_대상에_있다():
    """CDN 을 쓰지 않는다 — 망분리 환경에서 그대로 실패하기 때문이다."""
    from pathlib import Path

    import tot_dashboard.service as svc

    js = Path(svc.__file__).parent / "static" / "ugmap.js"
    assert js.is_file()
    assert "UGMap" in js.read_text(encoding="utf-8")
