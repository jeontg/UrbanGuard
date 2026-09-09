"""``load_roi_for_camera()`` — 침수 ROI 의 DB 우선 조회 (2026-08-22 전수점검).

## 왜 이 시험이 있나

웹 ROI 편집기(S-81)는 DB(``camera_rois``)에만 저장하는데, 침수 상시 탐지
파이프라인(``service/runner.py``)은 그동안 ``configs/roi/*.json`` 파일만
읽었다. **웹에서 ROI 를 새로 그려도 실제 판정에는 반영되지 않는 상태**였다
(화면 오버레이용 ``/api/roi/{block_id}`` 만 DB를 먼저 보고 있었다).

지켜야 할 것.

* DB에 있으면 **DB 값**을 쓴다 (파일이 있어도 DB가 이긴다)
* DB에 없으면 **파일**로 내려간다 (아직 이관 안 된 배포를 깨뜨리지 않는다)
* 둘 다 없으면 **빈 RoiConfig** — 판정이 멈추는 것보다 ROI 없이 도는 게 낫다
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.common.roi import load_roi_for_camera
from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi

FIXTURES = Path(__file__).parent / "fixtures"
CAM_ID = "ROIBRIDGE-1"

# DB에 넣을 값 — 파일과 **다른 모양**이라야 어느 쪽을 읽었는지 구분된다.
DB_ROI = {
    "frame_width": 800, "frame_height": 600,
    "shapes": {"road_roi": [[[10, 10], [200, 10], [200, 200], [10, 200]]]},
}


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(CameraRoi).where(CameraRoi.camera_id == CAM_ID))
        db.execute(sa_delete(CameraDomain).where(CameraDomain.camera_id == CAM_ID))
        db.execute(sa_delete(Camera).where(Camera.id == CAM_ID))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def _make_camera(*, with_roi: bool):
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": CAM_ID, "name": "ROI브리지시험",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/a.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        C.set_domains(db, cam, {"flood": {"enabled": True, "continuous": True}})
        if with_roi:
            _, errs = C.save_roi(db, CAM_ID, "flood", DB_ROI)
            assert not errs, errs
        db.commit()
    finally:
        db.close()


def test_DB에_있으면_DB값을_쓴다():
    """★ 핵심 — 파일이 있어도 DB가 이긴다."""
    _make_camera(with_roi=True)
    cfg = load_roi_for_camera(CAM_ID, "flood",
                              fallback_path=FIXTURES / "block_roi_config.json")
    assert cfg.frame_width == 800   # DB 값 (파일은 다른 크기)
    assert cfg.has_road
    assert cfg.road_roi[0][0] == [10, 10]


def test_DB에_없으면_파일로_내려간다():
    """아직 이관되지 않은 배포를 갑자기 깨뜨리지 않는다."""
    _make_camera(with_roi=False)
    cfg = load_roi_for_camera(CAM_ID, "flood",
                              fallback_path=FIXTURES / "block_roi_config.json")
    assert cfg.camera_name == "BLOCK-CHORYANG"  # 파일 값
    assert cfg.has_road


def test_카메라_자체가_없어도_파일로_내려간다():
    """DB에 등록조차 안 된 지점 — 예전 파일만 남아 있는 경우."""
    cfg = load_roi_for_camera("존재하지-않는-카메라", "flood",
                              fallback_path=FIXTURES / "block_roi_config.json")
    assert cfg.has_road


def test_둘_다_없으면_빈_설정():
    """판정이 멈추는 것보다 ROI 없이(=필터 없이) 도는 것이 낫다."""
    cfg = load_roi_for_camera("존재하지-않는-카메라", "flood",
                              fallback_path=FIXTURES / "없는파일.json")
    assert not cfg.has_road
    assert not cfg.has_low_point


def test_폴백_경로를_안_주면_빈_설정():
    cfg = load_roi_for_camera("존재하지-않는-카메라", "flood")
    assert not cfg.has_road


def test_flood_이외_도메인은_아직_DB를_안_본다():
    """``to_roi_config_dict()`` 가 flood 전용 변환기라서 — 교통·노면 ROI 는
    별도 경로(traffic_tracker / road.live_analyzer)를 쓴다. 지금은 폴백만
    동작하는 것이 **의도된 동작**임을 못박아 둔다."""
    _make_camera(with_roi=True)
    cfg = load_roi_for_camera(CAM_ID, "road",
                              fallback_path=FIXTURES / "block_roi_config.json")
    assert cfg.camera_name == "BLOCK-CHORYANG"  # DB가 아니라 파일
