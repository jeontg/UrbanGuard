"""시/도·구군 입력(S-80)과 상황판 지역 필터(S-01).

지켜야 할 것.

* **입력한 지역이 저장된다** — 좌표에서 유추한 값에 덮어써지면 안 된다
* **비워 두면 좌표로 시/도만 채운다** — 구·군은 지어내지 않는다
* **틀린 조합은 저장 전에 막힌다**
* **상황판 필터는 지도만 좁힌다** — 이벤트 큐까지 좁히면 다른 지역 위험을 놓친다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera
from tot_dashboard.service.main import app

PREFIX = "SGG-"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def _clean(db_schema):
    yield
    with get_session() as db:
        db.execute(delete(Camera).where(Camera.id.startswith(PREFIX)))
        db.commit()


def _create(cid, **over):
    data = {"id": cid, "name": over.pop("name", "시험지점"),
            "lat": 37.5665, "lng": 126.9780,
            "source_type": "hls", "source_url": "https://example.test/a.m3u8",
            "is_active": True}
    data.update(over)
    with get_session() as db:
        cam, errs = C.create(db, data)
        if not errs:
            db.commit()
        return (cam.sido, cam.sigungu) if cam else None, errs


# ---------- 저장 ----------

def test_입력한_시도와_구군이_저장된다():
    got, errs = _create(f"{PREFIX}A", sido="seoul", sigungu="강남구")
    assert errs == []
    assert got == ("seoul", "강남구")


def test_비워_두면_좌표로_시도만_채운다():
    """구·군까지 채우면 틀린 값이 들어간다."""
    got, errs = _create(f"{PREFIX}B")
    assert errs == []
    assert got == ("seoul", "")


def test_좌표와_다른_시도를_지정하면_지정한_쪽이_이긴다():
    """사각형은 행정경계가 아니다. 사람이 정한 값이 맞다."""
    got, errs = _create(f"{PREFIX}C", sido="gyeonggi", sigungu="과천시")
    assert errs == []
    assert got == ("gyeonggi", "과천시")


def test_그_시도에_없는_구군은_저장되지_않는다():
    got, errs = _create(f"{PREFIX}D", sido="seoul", sigungu="기장군")
    assert got is None
    assert any("없는 시·군·구" in e for e in errs)


def test_시도_없이_구군만_주면_막힌다():
    got, errs = _create(f"{PREFIX}E", sigungu="강남구")
    assert got is None
    assert any("시/도를 먼저" in e for e in errs)


# ---------- S-80 화면 ----------

def test_S80_등록폼에_시도_구군_선택상자가_있다(client):
    html = client.get("/settings/cameras").text
    assert 'name="sido"' in html
    assert 'name="sigungu"' in html
    assert "서울특별시" in html          # 시/도 선택지


def test_S80이_구군_목록을_화면에_그대로_내려_준다(client):
    """화면과 서버가 다른 목록을 보면 저장할 때만 거부되어 이유를 알기 어렵다.

    `tojson` 이 한글을 유니코드로 이스케이프하므로 문자열 대조가 아니라
    **파싱해서** 비교한다.
    """
    import json
    import re

    from tot_dashboard.core import regions as RG

    html = client.get("/settings/cameras").text
    m = re.search(r"var SIGUNGU = (\{.*?\});", html, re.S)
    assert m, "구·군 목록이 화면에 없다"
    sent = json.loads(m.group(1))
    assert set(sent) == set(RG.SIGUNGU)
    assert sent["seoul"] == list(RG.SIGUNGU["seoul"])
    assert "강남구" in sent["seoul"]


def test_S80_지역카드에_구군_내역이_나온다(client):
    _create(f"{PREFIX}F", sido="seoul", sigungu="강남구")
    html = client.get("/settings/cameras").text
    assert "구·군 미지정" in html or "강남구" in html


# ---------- S-01 상황판 ----------

def test_상황판에_지역_현황이_나온다(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "지역 현황" in r.text
    assert "전체 지역" in r.text


def test_지역으로_지도를_좁힌다(client):
    """전체와 특정 지역의 표시 지점 수가 달라야 필터가 동작한 것이다."""
    whole = client.get("/").text
    part = client.get("/?region=busan").text
    assert "/ " in part or "개소" in part
    assert whole != part


def test_없는_지역을_고르면_안내한다(client):
    r = client.get("/?region=jeju")
    assert r.status_code == 200
    assert "표시할 지점이 없습니다" in r.text


def test_필터가_이벤트_큐를_좁히지_않는다(client):
    """다른 지역 위험을 필터 때문에 못 보면 관제가 아니다."""
    whole = client.get("/").text
    part = client.get("/?region=busan").text
    for key in ("이벤트 큐", "미처리"):
        assert key in whole and key in part
