"""감시지점 등록(S-80)과 ROI 저장(S-81).

지점 하나가 잘못 들어가면 파이프라인 스레드가 기동 중 죽어 **다른 지점의
탐지까지 멈춘다.** 그래서 저장 전 검증이 핵심이다.
"""
from __future__ import annotations

import json

import pytest

from tot_dashboard.core import blocks_edit as BE
from tot_dashboard.core import roi_edit as RE

BASE = {"blocks": [{
    "id": "BLOCK-A", "name": "가지점", "dept": "도로과",
    "coordinates": {"lat": 35.15, "lng": 129.05},
    "source": {"type": "hls", "url": "https://x/playlist.m3u8"},
}]}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "blocks.json"
    p.write_text(json.dumps(BASE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TOT_BLOCKS_PATH", str(p))
    yield p
    monkeypatch.delenv("TOT_BLOCKS_PATH", raising=False)


def _new(**over):
    d = {"id": "BLOCK-B", "name": "나지점", "dept": "안전과",
         "lat": "35.16", "lng": "129.06",
         "source_type": "hls", "source_url": "https://y/playlist.m3u8"}
    d.update(over)
    return d


# --- 등록 -------------------------------------------------------------------
def test_create_adds_a_block(cfg):
    block, errs, _ = BE.create(_new())
    assert not errs and block["id"] == "BLOCK-B"
    assert len(BE.load()) == 2


def test_duplicate_id_is_rejected(cfg):
    _, errs, _ = BE.create(_new(id="BLOCK-A"))
    assert any("이미 존재" in e for e in errs)


@pytest.mark.parametrize("bad", ["block-b", "B", "블록", "1BLOCK", ""])
def test_malformed_id_is_rejected(cfg, bad):
    _, errs, _ = BE.create(_new(id=bad))
    assert errs


def test_missing_name_is_rejected(cfg):
    _, errs, _ = BE.create(_new(name="  "))
    assert any("지점명" in e for e in errs)


@pytest.mark.parametrize("lat,lng", [("99", "129"), ("35", "200"), ("x", "129")])
def test_coordinates_outside_korea_are_rejected(cfg, lat, lng):
    """범위를 벗어난 좌표가 들어가면 지도에서 나머지 지점이 한 점으로 뭉친다."""
    _, errs, _ = BE.create(_new(lat=lat, lng=lng))
    assert errs


def test_hls_source_requires_a_url(cfg):
    _, errs, _ = BE.create(_new(source_url=""))
    assert any("HLS" in e for e in errs)


def test_video_source_requires_a_path(cfg):
    _, errs, _ = BE.create(_new(source_type="video", source_url="", source_path=""))
    assert any("동영상" in e for e in errs)


def test_synthetic_source_needs_neither(cfg):
    _, errs, _ = BE.create(_new(source_type="synthetic", source_url=""))
    assert not errs


def test_nothing_is_written_when_validation_fails(cfg):
    before = cfg.read_text(encoding="utf-8")
    BE.create(_new(id="bad-id"))
    assert cfg.read_text(encoding="utf-8") == before


# --- 수정 -------------------------------------------------------------------
def test_update_changes_fields(cfg):
    block, errs, _ = BE.update("BLOCK-A", _new(id="IGNORED", name="새이름"))
    assert not errs and block["name"] == "새이름"


def test_update_keeps_the_original_id(cfg):
    """ID를 바꾸면 ROI 파일·이벤트 이력과의 연결이 끊긴다."""
    block, _, _ = BE.update("BLOCK-A", _new(id="BLOCK-ZZZ"))
    assert block["id"] == "BLOCK-A"


def test_update_preserves_unknown_fields(cfg):
    blocks = BE.load()
    blocks[0]["crowd"] = {"loiter_sec": 60}
    cfg.write_text(json.dumps({"blocks": blocks}, ensure_ascii=False), encoding="utf-8")
    block, _, _ = BE.update("BLOCK-A", _new(name="수정"))
    assert block["crowd"] == {"loiter_sec": 60}


def test_update_unknown_block(cfg):
    _, errs, _ = BE.update("NOPE", _new())
    assert errs


# --- 삭제 -------------------------------------------------------------------
def test_delete_removes_the_block(cfg):
    BE.create(_new())
    ok, errs, _ = BE.delete("BLOCK-A")
    assert ok and not errs
    assert [b["id"] for b in BE.load()] == ["BLOCK-B"]


def test_cannot_delete_the_last_block(cfg):
    """지점이 하나도 없으면 파이프라인이 기동하지 못한다."""
    ok, errs, _ = BE.delete("BLOCK-A")
    assert not ok and any("마지막" in e for e in errs)


# --- ROI --------------------------------------------------------------------
@pytest.fixture
def roi_dir(tmp_path, monkeypatch):
    d = tmp_path / "roi"
    d.mkdir()
    monkeypatch.setenv("URBANGUARD_ROI_DIR", str(d))
    yield d
    monkeypatch.delenv("URBANGUARD_ROI_DIR", raising=False)


def _payload(**over):
    p = {"frame_width": 320, "frame_height": 240,
         "road_roi": [[[10, 10], [100, 10], [100, 100]]],
         "low_point_roi": [], "lane_threshold_line": []}
    p.update(over)
    return p


def test_roi_round_trip(roi_dir):
    data, errs, _ = RE.save("BLOCK-A", _payload())
    assert not errs
    assert RE.load("BLOCK-A")["road_roi"] == data["road_roi"]


def test_road_roi_is_required(roi_dir):
    _, errs, _ = RE.save("BLOCK-A", _payload(road_roi=[]))
    assert any("도로 ROI" in e for e in errs)


def test_polygon_with_two_points_is_dropped(roi_dir):
    _, errs, _ = RE.save("BLOCK-A", _payload(road_roi=[[[1, 1], [2, 2]]]))
    assert errs  # 유효한 다각형이 하나도 남지 않으므로 실패


def test_coordinates_outside_the_frame_are_clamped(roi_dir):
    """프레임 밖 좌표는 마스크 연산에서 조용히 잘려 판정 영역이 달라진다."""
    data, errs, _ = RE.save("BLOCK-A", _payload(
        road_roi=[[[-50, -50], [999, 10], [100, 999]]]))
    assert not errs
    for x, y in data["road_roi"][0]:
        assert 0 <= x < 320 and 0 <= y < 240


def test_lane_line_must_have_exactly_two_points(roi_dir):
    _, errs, _ = RE.save("BLOCK-A", _payload(
        lane_threshold_line=[[1, 1], [2, 2], [3, 3]]))
    assert any("2개" in e for e in errs)


def test_lane_line_is_stored_flat(roi_dir):
    data, errs, _ = RE.save("BLOCK-A", _payload(
        lane_threshold_line=[[10, 20], [30, 40]]))
    assert not errs
    assert data["lane_threshold_line"] == [[10, 20], [30, 40]]


def test_zero_frame_size_is_rejected(roi_dir):
    _, errs, _ = RE.save("BLOCK-A", _payload(frame_width=0))
    assert errs


def test_status_reports_missing_roi(roi_dir):
    RE.save("BLOCK-A", _payload())
    st = RE.status(["BLOCK-A", "BLOCK-NONE"])
    assert st["BLOCK-A"]["road"] == 1
    assert st["BLOCK-NONE"]["exists"] is False


def test_saved_file_is_readable_by_the_existing_loader(roi_dir):
    """기존 파이프라인의 로더가 읽을 수 있어야 의미가 있다."""
    from tot_dashboard.common.roi import load_roi_config
    RE.save("BLOCK-A", _payload(lane_threshold_line=[[0, 100], [319, 120]]))
    cfg = load_roi_config(RE.roi_path("BLOCK-A"))
    assert cfg.has_road
    assert cfg.has_lane_line
