# -*- coding: utf-8 -*-
"""노면·인파 API가 whep_url을 접속 호스트 기준으로 내려주는가 —
2026-08-29(R-01) 위험 점검 후속.

## 왜 이 시험이 있나

WHEP 주소가 접속자와 무관하게 항상 127.0.0.1로 만들어져, 관제요원이
자기 PC에서 실시간 뷰를 열면 반드시 실패하던 문제(공개 호스트
``restream.public_host`` 미설정 시). ``core/restream.py::whep_url()``은
이미 ``request_host``를 받게 돼 있었지만, 노면(``road_cameras()``)·
인파(``api_crowd_continuous()``) 호출부가 아무도 안 넘겼다.

이 시험은 ``road_cameras(request_host=...)``가 실제로 그 호스트를
반영하는지, 넘기지 않으면(기존 호출부 회귀) 여전히 기본값으로
떨어지는지 고정한다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core import restream as RS
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain
from tot_dashboard.core.roles import Domain

CID = "TEST-WHEP-HOST-ROAD"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(CameraDomain).where(CameraDomain.camera_id == CID))
        db.execute(sa_delete(Camera).where(Camera.id == CID))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _setup(db_schema):
    RS.clear_health_cache()
    _purge()
    db = get_session()
    try:
        cam, errs = C.create(db, {
            "id": CID, "name": "WHEP호스트시험", "lat": "35.1", "lng": "129.0",
            "source_type": "hls", "source_url": "https://example.test/road.m3u8"})
        assert not errs, errs
        C.set_domains(db, cam, {Domain.ROAD.value: {"enabled": True, "continuous": False}})
        S.set_restream_enabled(db, True)
        # ⚠️ public_host를 다른 시험 파일이 DB에 남겨 뒀을 수 있다 —
        # request_host 반영을 확인하는 이 시험이 그 잔재 때문에 거짓
        # 실패하지 않도록 기본값으로 되돌린다.
        S.set_restream_hosts_ports(db, host="127.0.0.1", public_host="",
                                   rtsp_port="8554", whep_port="8889",
                                   api_port="9997")
        db.commit()
    finally:
        db.close()
    yield
    RS.clear_health_cache()
    S.invalidate()
    _purge()


def test_road_cameras는_request_host를_whep_url에_반영한다(monkeypatch):
    from tot_dashboard.service.main import road_cameras

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    cams = road_cameras(request_host="ug.example.internal")
    cam = next(c for c in cams if c["id"] == CID)
    assert cam["whep_url"] == "http://ug.example.internal:8889/TEST-WHEP-HOST-ROAD/whep"


def test_road_cameras는_request_host가_없으면_기존처럼_127_0_0_1이다(monkeypatch):
    """회귀 방지 — request_host 배선을 넣으며 기존(생략) 호출이 깨지면
    안 된다(예: api_road_blocks() 등 whep_url을 안 쓰는 호출부)."""
    from tot_dashboard.service.main import road_cameras

    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    cams = road_cameras()
    cam = next(c for c in cams if c["id"] == CID)
    assert cam["whep_url"] == "http://127.0.0.1:8889/TEST-WHEP-HOST-ROAD/whep"


# --- 침수·교통위험(경로 A) — 서빙 시점 재계산 -------------------------------
#
# _snapshot()이 백그라운드 스레드(request_host 없음)에서 캐시해 둔
# whep_url을, 서빙 시점(api_risk 등)에 이 요청의 접속 호스트로 다시
# 계산해 덮어쓰는지 고정한다. RiskStore를 직접 쓰지 않고
# `rewrite_whep_urls_in_place()`를 단위로 시험한다 — store.all()/get()이
# 매번 새 dict를 만든다는 계약은 이 함수의 전제일 뿐, 이 시험의 관심사가
# 아니다.
#
# ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 예전엔 이 함수가
# `service/main.py`에만 있었다. 침수·교통위험이 flood_service.py·
# traffic_service.py로 갈라지며 로직이 두 곳에 복제될 뻔해
# `core/restream.py`(공유 코드)로 옮겼다 — 두 서비스가 그대로
# 임포트해 쓴다. 그래서 이 시험도 공유 함수 하나만 확인하면 된다.

def test_rewrite_whep_urls는_request_host로_다시_계산한다(monkeypatch):
    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    blocks = [{"block_id": CID, "source_kind": "hls",
              "whep_url": "http://127.0.0.1:8889/TEST-WHEP-HOST-ROAD/whep"}]
    RS.rewrite_whep_urls_in_place(blocks, "ug.example.internal")
    assert blocks[0]["whep_url"] == "http://ug.example.internal:8889/TEST-WHEP-HOST-ROAD/whep"


def test_rewrite_whep_urls는_restream_unavailable이면_None으로_유지한다(monkeypatch):
    """재배포 서버 자체가 응답 없어 판정을 아예 안 돌린 상태다 — 호스트를
    바꿔도 재생 불가이므로 _snapshot()과 같은 판단(None)을 유지해야
    한다."""
    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    blocks = [{"block_id": CID, "source_kind": "restream_unavailable",
              "whep_url": "http://127.0.0.1:8889/TEST-WHEP-HOST-ROAD/whep"}]
    RS.rewrite_whep_urls_in_place(blocks, "ug.example.internal")
    assert blocks[0]["whep_url"] is None
