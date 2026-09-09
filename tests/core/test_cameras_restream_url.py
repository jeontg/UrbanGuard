# -*- coding: utf-8 -*-
"""``core/cameras.py::to_block_dict()``의 CCTV 재배포 허브 URL 치환
— 2026-08-28 신설.

## 왜 이 시험이 있나

침수+교통위험(``runner.py``)·노면(``road/live_analyzer.py``) 둘 다
``to_block_dict()``가 만든 ``block["source"]["url"]``을 그대로 신뢰한다
— 치환을 이 한 곳에만 두어 두 소비 지점을 건드리지 않으려는 설계다. 이
시험이 그 치환 로직 자체(켜짐/꺼짐, hls가 아닌 소스는 그대로 둠)를
고정한다.

★ 회귀 방지 — 브라우저(hls.js)는 재배포 주소(RTSP)를 재생할 수 없으므로,
치환 시 원본을 ``origin_url``에 남겨 둬야 한다(``service/runner.py::
_snapshot()``·``app.js``가 이 값으로 브라우저용 URL을 되돌린다).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core import restream as RS
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera

CID = "TEST-CAM-RESTREAM-URL"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(Camera).where(Camera.id == CID))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clean(db_schema):
    _purge()
    db = get_session()
    try:
        S.set_restream_enabled(db, False)
        S.set_restream_excluded_ids(db, [])
        db.commit()
    finally:
        db.close()
    S.invalidate()
    yield
    _purge()
    db = get_session()
    try:
        S.set_restream_enabled(db, False)
        S.set_restream_excluded_ids(db, [])
        db.commit()
    finally:
        db.close()
    S.invalidate()


def _make_hls_camera():
    db = get_session()
    try:
        cam, errs = C.create(db, {
            "id": CID, "name": "재배포URL시험", "lat": "35.1", "lng": "129.0",
            "source_type": "hls", "source_url": "https://example.test/a.m3u8"})
        assert not errs, errs
        db.commit()
    finally:
        db.close()


def test_재배포_꺼져있으면_원본_URL_그대로다():
    _make_hls_camera()
    db = get_session()
    try:
        block = C.to_block_dict(C.get(db, CID))
    finally:
        db.close()
    assert block["source"]["url"] == "https://example.test/a.m3u8"
    assert "origin_url" not in block["source"]


def test_재배포_켜져있으면_RTSP로_치환되고_원본은_origin_url에_남는다():
    _make_hls_camera()
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    db = get_session()
    try:
        block = C.to_block_dict(C.get(db, CID))
    finally:
        db.close()
    assert block["source"]["url"] == RS.rtsp_url(CID)
    assert block["source"]["origin_url"] == "https://example.test/a.m3u8"


def test_재배포_켜져있어도_제외_목록에_있으면_원본_URL_그대로다():
    """★ 2026-08-28 실기 검증 중 발견 — 부산시 ITS 원본 일부(Wowza 계열)가
    MediaMTX의 표준 HLS 폴링과 근본적으로 안 맞는 것을 확인했다. 그런
    카메라는 재배포가 전역으로 켜져 있어도 예외로 원본 직결을 유지해야
    한다."""
    _make_hls_camera()
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_excluded_ids(db, [CID])
        db.commit()
    finally:
        db.close()

    db = get_session()
    try:
        block = C.to_block_dict(C.get(db, CID))
    finally:
        db.close()
    assert block["source"]["url"] == "https://example.test/a.m3u8"
    assert "origin_url" not in block["source"]


def test_video_소스는_재배포가_켜져도_영향받지_않는다():
    db = get_session()
    try:
        cam, errs = C.create(db, {
            "id": CID, "name": "재배포URL시험(동영상)", "lat": "35.1", "lng": "129.0",
            "source_type": "video", "source_path": "uploads/a.mp4"})
        assert not errs, errs
        db.commit()
    finally:
        db.close()
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    db = get_session()
    try:
        block = C.to_block_dict(C.get(db, CID))
    finally:
        db.close()
    assert block["source"]["path"] == "uploads/a.mp4"
    assert "url" not in block["source"]
    assert "origin_url" not in block["source"]


def test_재배포_설정_조회_자체가_실패해도_원본_URL로_안전하게_진행한다(monkeypatch):
    """설정 캐시 조회 자체가 예상 밖으로 예외를 던지는 극단적인 경우를
    흉내낸다 — 이 조회 하나 때문에 블록 구성 전체가 죽으면 안 된다(다른
    설정 조회 실패 처리와 같은 원칙)."""
    _make_hls_camera()

    def _boom(*a, **k):
        raise RuntimeError("설정 조회 실패(시험)")

    monkeypatch.setattr(S, "restream_enabled", _boom)
    db = get_session()
    try:
        block = C.to_block_dict(C.get(db, CID))
    finally:
        db.close()
    assert block["source"]["url"] == "https://example.test/a.m3u8"
