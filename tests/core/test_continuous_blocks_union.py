# -*- coding: utf-8 -*-
"""``continuous_blocks_any()`` / ``to_block_dict()``의 ``flood_enabled`` —
2026-08-23 실사용 중 발견.

## 왜 이 시험이 있나

사용자가 실제로 겪은 순서: CCTV 관리(S-80)에서 「범내골교차로」의
**교통위험만** 「사용+상시」로 켜고(침수는 미사용) 서비스를 재기동했더니,
**교통위험 실시간 관제 화면에 그 지점이 아예 안 보였다.**

원인 — 상시 처리 루프(``PipelineRunner``)가 만드는 대상 목록이 그동안
``continuous_blocks(db, "flood")`` **하나만** 봤다. 침수가 미사용이면
그 카메라는 처리 루프 자체에 안 올라가므로, 교통위험 판정도 전혀 돌지
않았다(``/api/risk``가 그 루프의 결과만 돌려준다).

지켜야 할 것.

* 침수·교통위험 **둘 중 하나만** 상시여도 대상에 포함된다(합집합)
* 카메라가 **둘 다** 상시여도 블록은 **한 번만** 생긴다(중복 금지)
* 「탐지 지정이 꺼져도 ROI는 보관」 원칙 때문에 ``enabled=False``인
  도메인도 ROI를 갖고 있을 수 있다 — ``flood_enabled``는 반드시
  **실제 지정 여부**만 보고, ROI 존재 여부와 섞이면 안 된다(섞이면
  침수를 꺼 둔 카메라에서 조용히 침수 판정이 다시 돌 수 있다)
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi
from tot_dashboard.core.roles import Domain

CAM_TRAFFIC_ONLY = "UNIONTEST-TRAFFIC-ONLY"
CAM_BOTH = "UNIONTEST-BOTH"
CAM_NEITHER = "UNIONTEST-NEITHER"
ALL_IDS = (CAM_TRAFFIC_ONLY, CAM_BOTH, CAM_NEITHER)

_CONGESTION_ROI = {"frame_width": 640, "frame_height": 360,
                   "shapes": {"congestion_roi": [[[0, 0], [100, 0], [100, 100]]]}}


def _purge():
    db = get_session()
    try:
        for cid in ALL_IDS:
            db.execute(sa_delete(CameraRoi).where(CameraRoi.camera_id == cid))
            db.execute(sa_delete(CameraDomain).where(CameraDomain.camera_id == cid))
            db.execute(sa_delete(Camera).where(Camera.id == cid))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def _make(cam_id: str, *, flood_enabled: bool, flood_continuous: bool,
          traffic_enabled: bool, traffic_continuous: bool,
          keep_flood_roi: bool = False):
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": cam_id, "name": f"{cam_id}지점",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/a.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        C.set_domains(db, cam, {
            "flood": {"enabled": flood_enabled, "continuous": flood_continuous},
            "traffic": {"enabled": traffic_enabled, "continuous": traffic_continuous},
        })
        if keep_flood_roi:
            _, errs = C.save_roi(db, cam_id, "flood",
                                 {"frame_width": 640, "frame_height": 360,
                                  "shapes": {"road_roi": [[[0, 0], [50, 0], [50, 50]]]}})
            assert not errs, errs
        _, errs = C.save_roi(db, cam_id, "traffic", _CONGESTION_ROI)
        assert not errs, errs
        db.commit()
    finally:
        db.close()


def test_교통위험만_상시여도_대상에_포함된다():
    """★ 이번 회차의 핵심 — 사용자가 실제로 겪은 상황 재현."""
    _make(CAM_TRAFFIC_ONLY, flood_enabled=False, flood_continuous=False,
         traffic_enabled=True, traffic_continuous=True,
         keep_flood_roi=True)  # 예전에 침수 ROI를 그려 둔 적 있는 카메라

    db = get_session()
    try:
        blocks = C.continuous_blocks_any(
            db, (Domain.FLOOD.value, Domain.TRAFFIC.value))
    finally:
        db.close()

    b = next((x for x in blocks if x["id"] == CAM_TRAFFIC_ONLY), None)
    assert b is not None, "교통위험만 상시인 카메라가 대상 목록에서 빠졌다"
    assert "traffic" in b, "교통 설정이 블록에 실리지 않았다"
    # ROI는 보관돼 있어도 지정 자체는 꺼져 있으므로 침수 판정은 돌면 안 된다.
    assert b["flood_enabled"] is False, (
        "침수 ROI가 보관 중이라고 flood_enabled 가 True 로 새면 안 된다 "
        "— 지정을 껐는데 침수 판정이 조용히 다시 돌게 된다")
    assert b["traffic_enabled"] is True, "교통위험만 켰는데 traffic_enabled 가 False다"


def test_둘_다_상시여도_블록은_한_번만_생긴다():
    _make(CAM_BOTH, flood_enabled=True, flood_continuous=True,
         traffic_enabled=True, traffic_continuous=True)

    db = get_session()
    try:
        blocks = C.continuous_blocks_any(
            db, (Domain.FLOOD.value, Domain.TRAFFIC.value))
    finally:
        db.close()

    matches = [x for x in blocks if x["id"] == CAM_BOTH]
    assert len(matches) == 1, "합집합 처리에서 카메라가 중복 생성됐다"
    assert matches[0]["flood_enabled"] is True
    assert matches[0]["traffic_enabled"] is True
    assert "traffic" in matches[0]


def test_둘_다_아니면_대상에서_빠진다():
    _make(CAM_NEITHER, flood_enabled=False, flood_continuous=False,
         traffic_enabled=False, traffic_continuous=False)

    db = get_session()
    try:
        blocks = C.continuous_blocks_any(
            db, (Domain.FLOOD.value, Domain.TRAFFIC.value))
    finally:
        db.close()

    assert all(x["id"] != CAM_NEITHER for x in blocks)


def test_flood_enabled_플래그는_실제_지정_여부만_본다():
    """탐지 지정이 켜져 있으면(상시 여부와 무관) True 여야 한다."""
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": CAM_BOTH + "-B", "name": "임시",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/b.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        C.set_domains(db, cam, {"flood": {"enabled": True, "continuous": False}})
        db.commit()
        block = C.to_block_dict(cam)
        assert block["flood_enabled"] is True
    finally:
        db.execute(sa_delete(CameraDomain).where(
            CameraDomain.camera_id == CAM_BOTH + "-B"))
        db.execute(sa_delete(Camera).where(Camera.id == CAM_BOTH + "-B"))
        db.commit()
        db.close()


def test_traffic_enabled_플래그는_실제_지정_여부만_본다():
    """★ 2026-08-28 — flood_enabled 와 대칭인 값. 「탐지 지정이 꺼져도
    ROI는 보관」 원칙 때문에 침수만 상시로 켠 카메라도 예전에 그려 둔
    교통 ROI를 갖고 있을 수 있다 — ROI 존재 여부와 섞이면 안 된다."""
    cam_id = CAM_BOTH + "-C"
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": cam_id, "name": "임시",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/c.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        # 침수만 상시로 켜고, 교통은 지정 자체를 끈다 — 다만 예전에 그려
        # 둔 교통 ROI 는 보관된 상태(keep_flood_roi 와 대칭 시나리오).
        C.set_domains(db, cam, {
            "flood": {"enabled": True, "continuous": True},
            "traffic": {"enabled": False, "continuous": False},
        })
        _, errs = C.save_roi(db, cam_id, "traffic", _CONGESTION_ROI)
        assert not errs, errs
        db.commit()
        block = C.to_block_dict(cam)
        assert block["flood_enabled"] is True
        assert block["traffic_enabled"] is False, (
            "교통 ROI가 보관 중이라고 traffic_enabled 가 True 로 새면 안 된다 "
            "— 지정을 안 했는데 교통위험 판정이 조용히 돌게 된다")
    finally:
        db.execute(sa_delete(CameraRoi).where(CameraRoi.camera_id == cam_id))
        db.execute(sa_delete(CameraDomain).where(CameraDomain.camera_id == cam_id))
        db.execute(sa_delete(Camera).where(Camera.id == cam_id))
        db.commit()
        db.close()
