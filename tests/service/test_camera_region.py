"""S-80 지역별 등록 현황과 일괄 해지.

지켜야 할 것.

* **지역은 좌표에서 판정한다** — 따로 저장하면 좌표를 고쳤을 때 어긋난다
* **확인 문구 없이는 해지되지 않는다** — 버튼 하나로 수십 대가 사라지면 안 된다
* **전부 해지할 수는 없다** — 한 대도 없으면 「설정이 날아간 것」과 구분되지 않는다
* **해지는 감사 로그에 남는다**
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog, Camera
from tot_dashboard.service.main import app

PREFIX = "RGN-"


@pytest.fixture(scope="module")
def anon_client():
    # 수명주기를 켜지 않는다 — runner 는 프로세스당 한 번만 start 할 수 있다.
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def _add(cid: str, name: str, lat: float, lng: float) -> None:
    with get_session() as db:
        cam, errs = C.create(db, {
            "id": cid, "name": name, "lat": lat, "lng": lng,
            "source_type": "hls", "source_url": "https://example.test/a.m3u8",
            "is_active": True})
        assert not errs, errs
        db.commit()


@pytest.fixture(autouse=True)
def _seed(db_schema):
    """시험용 지점을 넣고 끝나면 지운다. 다른 시험의 지점은 건드리지 않는다.

    ``db_schema`` 를 받는 이유는 순서다. 이것이 없으면 테이블이 생기기 전에
    지점을 넣으려다 죽는다.
    """
    _add(f"{PREFIX}SEOUL1", "시험 서울1", 37.5665, 126.9780)
    _add(f"{PREFIX}SEOUL2", "시험 서울2", 37.5400, 127.0000)
    _add(f"{PREFIX}JEJU1", "시험 제주1", 33.4996, 126.5312)
    yield
    with get_session() as db:
        db.execute(delete(Camera).where(Camera.id.startswith(PREFIX)))
        db.commit()


# ---------- 판정과 묶기 ----------

def test_좌표로_지역을_판정한다():
    with get_session() as db:
        assert C.region_of(C.get(db, f"{PREFIX}SEOUL1")) == "seoul"
        assert C.region_of(C.get(db, f"{PREFIX}JEJU1")) == "jeju"


def test_지역별로_묶인다():
    with get_session() as db:
        groups = {g["label"]: g["count"] for g in C.group_by_region(C.list_all(db))}
    assert groups.get("서울특별시", 0) >= 2
    assert groups.get("제주특별자치도", 0) >= 1


def test_by_region이_해당_지역만_준다():
    with get_session() as db:
        ids = {c.id for c in C.by_region(db, "jeju")}
    assert f"{PREFIX}JEJU1" in ids
    assert f"{PREFIX}SEOUL1" not in ids


# ---------- 화면 ----------

def test_S80_화면에_지역별_현황이_나온다(client):
    html = client.get("/settings/cameras").text
    assert "지역별 등록 현황" in html
    assert "서울특별시" in html
    assert "제주특별자치도" in html


def _row_count(client, url: str) -> int:
    """목록 표의 데이터 행 수. 지점 이름 칸의 표시가 행마다 하나씩 나온다."""
    import re
    html = client.get(url).text
    body = html.split("등록된 CCTV", 1)[1]
    return len(re.findall(r'<tr>\s*<td>\s*<div style="font-weight:700"', body))


def test_지역을_고르면_그_지역만_보인다(client):
    whole = _row_count(client, "/settings/cameras")
    seoul = _row_count(client, "/settings/cameras?region=seoul")
    jeju = _row_count(client, "/settings/cameras?region=jeju")
    assert seoul >= 2                      # 시험용 서울 지점 2대
    assert seoul < whole                   # 전체보다는 적다
    assert jeju >= 1                       # 시험용 제주 지점 1대
    assert seoul + jeju <= whole


def test_구군까지_좁힐_수_있다(client):
    seoul = _row_count(client, "/settings/cameras?region=seoul")
    unset = _row_count(client, "/settings/cameras?region=seoul&sigungu=none")
    assert unset <= seoul                  # 미지정은 서울의 부분집합


def test_지역_미상도_고를_수_있다(client):
    """빈 값은 「전체」와 구분되지 않아 none 이라는 값을 따로 둔다."""
    r = client.get("/settings/cameras?region=none")
    assert r.status_code == 200
    assert "지역 미상" in r.text


def test_거른_뒤에도_전체_수를_함께_보여_준다(client):
    """몇 개 중 몇 개를 보고 있는지 모르면 목록이 빈 것처럼 보인다."""
    html = client.get("/settings/cameras?region=seoul").text
    assert "개소" in html
    assert "전체 보기" in html


def test_지점이_없는_지역은_안내한다(client):
    html = client.get("/settings/cameras?region=gwangju").text
    assert "등록된 CCTV가 없습니다" in html
    assert "광주광역시" in html


def test_지역_현황_카드가_필터로_연결된다(client):
    html = client.get("/settings/cameras").text
    assert "/settings/cameras?region=" in html


def test_S80_화면에_서울_수집_버튼이_있다(client):
    html = client.get("/settings/cameras").text
    assert "/settings/cameras/import/seoul" in html
    assert "키 불필요" in html


# ---------- 해지 ----------

def test_확인_문구가_틀리면_해지되지_않는다(client):
    r = client.post("/settings/cameras/region/delete",
                    data={"region": "jeju", "confirm": "제주"})
    assert r.status_code == 400
    assert "제주특별자치도" in r.text
    with get_session() as db:
        assert C.get(db, f"{PREFIX}JEJU1") is not None


def test_확인_문구가_맞으면_해지된다(client):
    r = client.post("/settings/cameras/region/delete",
                    data={"region": "jeju", "confirm": "제주특별자치도"})
    assert r.status_code == 200
    with get_session() as db:
        assert C.get(db, f"{PREFIX}JEJU1") is None
        assert C.get(db, f"{PREFIX}SEOUL1") is not None      # 다른 지역은 그대로


def test_해지는_감사_로그에_남는다(client):
    client.post("/settings/cameras/region/delete",
                data={"region": "jeju", "confirm": "제주특별자치도"})
    with get_session() as db:
        rows = list(db.scalars(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(10)).all())
    assert any("지역 해지" in (r.target or "") for r in rows)


def test_지점이_없는_지역은_안내한다(client):
    r = client.post("/settings/cameras/region/delete",
                    data={"region": "gwangju", "confirm": "광주광역시"})
    assert r.status_code == 400
    assert "등록된 지점이 없습니다" in r.text


def test_전부_해지할_수는_없다():
    """마지막 한 대까지 지우면 화면·파이프라인이 빈 채로 돈다."""
    with get_session() as db:
        only = {C.region_of(c) for c in C.list_all(db)}
        if len(only) != 1:
            pytest.skip("여러 지역이 등록되어 있어 이 상황을 만들 수 없다")
        count, errs = C.delete_region(db, only.pop() or "")
    assert count == 0
    assert errs
