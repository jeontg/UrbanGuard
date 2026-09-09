# -*- coding: utf-8 -*-
"""CCTV 재배포 허브(MediaMTX) 경로가 카메라 CRUD와 함께 즉시 동기화되는가
— 2026-08-28 신설.

## 왜 이 시험이 있나

``core/restream.py::add_path()``/``remove_path()``는 실패를 삼키도록
설계했다 — 재배포는 부가 기능이지 카메라 등록의 필수 조건이 아니다. 이
계약이 실제로 지켜지는지, 즉 **MediaMTX 호출이 예외를 던져도 카메라
생성·수정·삭제 자체는 정상 처리되는지**가 이 시험의 핵심 회귀 대상이다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tot_dashboard.core import restream as RS
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain
from tot_dashboard.service.main import app

CID = "TEST-RESTREAM-SYNC-A"
# ``core/cameras.py::delete()``가 "마지막 남은 카메라는 삭제할 수 없습니다"
# 로 막는다 — 삭제 시험이 그 안전장치에 걸리지 않도록 곁다리 카메라를
# 하나 더 둔다(이 카메라 자체는 시험 대상이 아니다).
CID_OTHER = "TEST-RESTREAM-SYNC-OTHER"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def _purge():
    db = get_session()
    try:
        for cid in (CID, CID_OTHER):
            db.execute(delete(CameraDomain).where(CameraDomain.camera_id == cid))
            db.execute(delete(Camera).where(Camera.id == cid))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean(db_schema):
    from tot_dashboard.core import cameras as C
    from tot_dashboard.core import settings as S

    _purge()
    db = get_session()
    try:
        _, errs = C.create(db, {
            "id": CID_OTHER, "name": "재배포동기화시험(곁다리)",
            "lat": "35.1", "lng": "129.0", "source_type": "hls",
            "source_url": "https://example.test/other.m3u8"})
        assert not errs, errs
        db.commit()
    finally:
        db.close()
    yield
    _purge()
    # ★ restream.enabled 는 이 시험 파일 전용 카메라와 무관한 전역 설정이다
    # — 다른 시험 파일이 "기본 꺼짐"을 전제로 하므로 여기서 반드시 되돌린다.
    db = get_session()
    try:
        S.set_restream_enabled(db, False)
        db.commit()
    finally:
        db.close()
    S.invalidate()


def _create_form(**over):
    form = {"id": CID, "name": "재배포동기화시험", "lat": "35.1", "lng": "129.0",
            "source_type": "hls", "source_url": "https://example.test/a.m3u8"}
    form.update(over)
    return form


def test_카메라_등록_시_재배포_경로_추가를_시도한다(client, monkeypatch):
    calls = []
    monkeypatch.setattr(RS, "add_path",
                        lambda cid, url, db=None: calls.append((cid, url)) or True)
    r = client.post("/settings/cameras/create", data=_create_form())
    assert r.status_code == 200
    assert calls == [(CID, "https://example.test/a.m3u8")]


def test_MediaMTX가_응답하지_않아도_카메라_등록은_성공한다(client, monkeypatch):
    """★ 핵심 회귀 — 재배포는 부가 기능이다. 실제 네트워크 경계
    (``urllib.request.urlopen``)에서 연결이 거부돼도 라우트를 거쳐 카메라
    등록 자체는 200으로 성공해야 한다. ``add_path()``를 다시 mock하지
    않고 실제 경계에서 끊어, "라우트가 우연히 add_path를 안 불러서
    통과한" 거짓 양성을 배제한다."""
    from tot_dashboard.core import settings as S

    db = get_session()
    try:
        S.set_restream_enabled(db, True)  # 꺼져 있으면 add_path가 HTTP를 아예 안 불러 시험 의미가 없다
        db.commit()
    finally:
        db.close()

    def _boom(*a, **k):
        raise ConnectionRefusedError("MediaMTX 응답 없음")

    monkeypatch.setattr(RS.urllib.request, "urlopen", _boom)
    r = client.post("/settings/cameras/create", data=_create_form())
    assert r.status_code == 200
    check_db = get_session()
    try:
        assert check_db.get(Camera, CID) is not None, "재배포 호출 실패로 카메라 등록 자체가 막혔다"
    finally:
        check_db.close()


def test_카메라_수정_시에도_재배포_경로를_재동기화한다(client, monkeypatch):
    r = client.post("/settings/cameras/create", data=_create_form())
    assert r.status_code == 200

    calls = []
    monkeypatch.setattr(RS, "add_path",
                        lambda cid, url, db=None: calls.append((cid, url)) or True)
    r = client.post(f"/settings/cameras/{CID}/update",
                    data=_create_form(source_url="https://example.test/b.m3u8"))
    assert r.status_code == 200
    assert calls == [(CID, "https://example.test/b.m3u8")]


def test_카메라_삭제_시_재배포_경로를_지운다(client, monkeypatch):
    r = client.post("/settings/cameras/create", data=_create_form())
    assert r.status_code == 200

    calls = []
    monkeypatch.setattr(RS, "remove_path",
                        lambda cid, db=None: calls.append(cid) or True)
    r = client.post(f"/settings/cameras/{CID}/delete")
    assert r.status_code == 200
    assert calls == [CID]
