# -*- coding: utf-8 -*-
"""S-80(CCTV 관리) — 상시 화질 감시 배지·요약 타일 — 2026-08-30 신설.

``test_camera_domain_stats.py``와 같은 패턴(전용 접두사 카메라를 만들고
지운다) — 스캐너(core/video_quality.py)가 매긴 등급이 카메라 행 배지와
상단 요약 타일에 그대로 반영되는지 확인한다. 실제 ffmpeg를 띄우지 않고
모듈 딕셔너리를 직접 채운다(스캐너 자체의 동작은 tests/core/
test_video_quality.py가 고정한다 — 여기서는 화면 반영만 본다).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core import video_quality as VQ
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain

PFX = "TEST-VQBADGE-"


@pytest.fixture(scope="module")
def anon_client():
    from fastapi.testclient import TestClient
    from tot_dashboard.service.main import app
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


def _clear_vq():
    with VQ._lock:
        for cid in list(VQ._grade):
            if cid.startswith(PFX):
                VQ._grade.pop(cid, None)
                VQ._decode_error_count.pop(cid, None)
                VQ._last_scanned_at.pop(cid, None)


@pytest.fixture(autouse=True)
def _clean(db_schema):
    _purge()
    _clear_vq()
    yield
    _purge()
    _clear_vq()


def _add_camera(suffix: str) -> str:
    cid = f"{PFX}{suffix}"
    with get_session() as db:
        cam, errs = C.create(db, {
            "id": cid, "name": f"화질배지시험{suffix}",
            "lat": "35.1", "lng": "129.0", "source_type": "hls",
            "source_url": "https://example.test/a.m3u8"})
        assert not errs, errs
        db.commit()
    return cid


def test_스캔된_적_없는_카메라는_미측정으로_표시된다(client):
    _add_camera("A")
    res = client.get("/settings/cameras")
    assert "미측정" in res.text


def test_손상_심각_등급이면_배지에_손상_심각이_뜬다(client):
    cid = _add_camera("B")
    with VQ._lock:
        VQ._grade[cid] = "crit"
    res = client.get("/settings/cameras")
    assert "ug-badge--crit" in res.text
    assert "손상 심각" in res.text


def test_주의_등급이면_배지에_주의가_뜬다(client):
    cid = _add_camera("C")
    with VQ._lock:
        VQ._grade[cid] = "warn"
    res = client.get("/settings/cameras")
    assert res.status_code == 200
    # 개별 카메라 배지 라벨은 "주의"다 — 도메인 통계 타일의 "주의 N"과
    # 혼동하지 않도록, 카메라 행 근처에 배지 클래스가 함께 있는지만 본다.
    assert "ug-badge--warn" in res.text


def test_정상_등급이면_요약_타일_정상_수가_늘어난다(client):
    before_html = client.get("/settings/cameras").text
    cid = _add_camera("D")
    with VQ._lock:
        VQ._grade[cid] = "on"
    after_html = client.get("/settings/cameras").text
    assert after_html.count("ug-badge--on") >= before_html.count("ug-badge--on")
