# -*- coding: utf-8 -*-
"""S-80(CCTV 관리) — "등록된 CCTV 00개소" 옆에 4개 도메인별 사용 현황
통계 — 2026-08-29 신설.

## 왜 이 화면이 필요한가

"등록된 CCTV 00개소"는 **등록 수**일 뿐, 그중 몇 곳이 실제로 침수·
교통위험·인파관리·노면 4개 탐지서비스에 쓰이는지는 알 수 없었다.
관제 부서마다 담당 도메인의 실사용 현황을 한눈에 봐야 한다.

지켜야 할 것.

* **지역 필터와 무관하게 항상 전체 기준** — 특정 지역만 보고 있어도
  시스템 전체 사용 현황은 그대로 보여야 판단할 수 있다
  (`routes_cameras.py::_list_page` 참고, `region_groups`와 같은 원칙)
* **도메인마다 사용(enabled) = 상시(continuous) + 선택(selective)**으로
  나뉜다 — 상시/선택 두 숫자를 더하면 반드시 사용 숫자와 같아야 한다
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain
from tot_dashboard.core.roles import Domain
from tot_dashboard.service.main import app

PFX = "TEST-DOMSTAT-"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def _purge():
    with get_session() as db:
        db.execute(delete(CameraDomain).where(CameraDomain.camera_id.like(f"{PFX}%")))
        db.execute(delete(Camera).where(Camera.id.like(f"{PFX}%")))
        db.commit()


@pytest.fixture(autouse=True)
def _clean(db_schema):
    _purge()
    yield
    _purge()


def _add_camera(suffix: str, flood: dict | None = None) -> str:
    cid = f"{PFX}{suffix}"
    with get_session() as db:
        cam, errs = C.create(db, {
            "id": cid, "name": f"도메인통계시험{suffix}",
            "lat": "35.1", "lng": "129.0", "source_type": "hls",
            "source_url": "https://example.test/a.m3u8"})
        assert not errs, errs
        if flood is not None:
            C.set_domains(db, cam, {Domain.FLOOD.value: flood})
        db.commit()
    return cid


_TILE_RE_TEMPLATE = (
    r'{label}</div>\s*<div[^>]*>\s*(\d+)<span[^>]*>개소</span></div>\s*'
    r'<div[^>]*>\s*<span[^>]*>상시\s+(\d+)</span>\s*'
    r'<span[^>]*>선택\s+(\d+)</span>')


def _read_stat(html: str, label: str) -> dict:
    m = re.search(_TILE_RE_TEMPLATE.format(label=re.escape(label)), html, re.DOTALL)
    assert m, f"'{label}' 통계 타일을 화면에서 못 찾았다"
    return {"enabled": int(m.group(1)), "continuous": int(m.group(2)),
            "selective": int(m.group(3))}


def test_상시로_켠_카메라가_침수_상시_수에_반영된다(client):
    before = _read_stat(client.get("/settings/cameras").text, "침수")
    _add_camera("A", flood={"enabled": True, "continuous": True})
    after = _read_stat(client.get("/settings/cameras").text, "침수")

    assert after["enabled"] == before["enabled"] + 1
    assert after["continuous"] == before["continuous"] + 1
    assert after["selective"] == before["selective"]


def test_선택으로_켠_카메라가_침수_선택_수에_반영된다(client):
    before = _read_stat(client.get("/settings/cameras").text, "침수")
    _add_camera("B", flood={"enabled": True, "continuous": False})
    after = _read_stat(client.get("/settings/cameras").text, "침수")

    assert after["enabled"] == before["enabled"] + 1
    assert after["continuous"] == before["continuous"]
    assert after["selective"] == before["selective"] + 1


def test_미사용_카메라는_어느_도메인_수에도_반영되지_않는다(client):
    before = _read_stat(client.get("/settings/cameras").text, "침수")
    _add_camera("C", flood={"enabled": False, "continuous": False})
    after = _read_stat(client.get("/settings/cameras").text, "침수")

    assert after == before


def test_사용_숫자는_상시와_선택의_합과_같다(client):
    """회귀 방지 — 세 숫자가 서로 어긋나면 화면을 보는 사람이 혼란스럽다."""
    _add_camera("D", flood={"enabled": True, "continuous": True})
    _add_camera("E", flood={"enabled": True, "continuous": False})
    stat = _read_stat(client.get("/settings/cameras").text, "침수")
    assert stat["enabled"] == stat["continuous"] + stat["selective"]


def test_지역_필터를_걸어도_통계는_전체_기준이다(client):
    """지역 필터와 무관하게 항상 전체 기준(region_groups와 같은 원칙)."""
    whole = _read_stat(client.get("/settings/cameras").text, "침수")
    _add_camera("F", flood={"enabled": True, "continuous": True})
    # 이 시험 카메라들은 lat/lng 35.1/129.0(부산 인근)이라 "seoul" 필터에는
    # 안 걸린다 — 그런데도 통계는 걸러지지 않고 전체 기준으로 나와야 한다.
    filtered = _read_stat(client.get("/settings/cameras?region=seoul").text, "침수")
    assert filtered["enabled"] == whole["enabled"] + 1
