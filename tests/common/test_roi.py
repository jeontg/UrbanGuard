"""Verify common/roi.py reads both the legacy single-polygon format
(underpath_flood_dashboard's config/roi_config.json) and the newer
multi-polygon format (flood3's configs/roi/BLOCK-*.json).
"""
from pathlib import Path

from tot_dashboard.common.roi import load_roi_config, point_in_polygons, polygon_mask

FIXTURES = Path(__file__).parent / "fixtures"


def test_loads_legacy_single_polygon_format():
    cfg = load_roi_config(FIXTURES / "legacy_roi_config.json")
    assert cfg.camera_name == "underpath_01"
    assert cfg.has_road
    # legacy format is a flat point list -> normalized to a one-item polygon list
    assert len(cfg.road_roi) == 1
    assert len(cfg.road_roi[0]) >= 3


def test_loads_block_multi_polygon_format():
    cfg = load_roi_config(FIXTURES / "block_roi_config.json")
    assert cfg.camera_name == "BLOCK-CHORYANG"
    assert cfg.has_road


def test_polygon_mask_and_point_in_polygons_roundtrip():
    cfg = load_roi_config(FIXTURES / "legacy_roi_config.json")
    h, w = cfg.frame_height, cfg.frame_width
    mask = polygon_mask(cfg.road_roi, h, w)
    assert mask.shape == (h, w)
    assert mask.sum() > 0
    # pick the median masked-in pixel (robust to the polygon being non-convex;
    # avoids picking a boundary pixel where fillPoly's rasterization and
    # pointPolygonTest's precise geometric test can disagree by a hair)
    ys, xs = mask.nonzero()
    mid = len(xs) // 2
    inside_point = (float(xs[mid]), float(ys[mid]))
    assert point_in_polygons(inside_point, cfg.road_roi) is True
    assert point_in_polygons((-100, -100), cfg.road_roi) is False


# --- scale_polygons (2026-08-22) ---------------------------------------------
# ROI 는 정지영상 픽셀 좌표로 저장되는데 실제 캡처 해상도가 다를 수 있다.
def test_scale_polygons_배율을_적용한다():
    from tot_dashboard.common.roi import scale_polygons
    out = scale_polygons([[[0, 0], [100, 50]]], (200, 100), (400, 200))
    assert out == [[[0, 0], [200, 100]]]


def test_scale_polygons_크기가_같으면_그대로():
    from tot_dashboard.common.roi import scale_polygons
    src = [[[10, 20], [30, 40]]]
    assert scale_polygons(src, (640, 480), (640, 480)) == src


def test_scale_polygons_크기를_모르면_원본을_그대로_돌려준다():
    """★ 잘못 늘이는 것보다 그대로 두는 편이 낫다."""
    from tot_dashboard.common.roi import scale_polygons
    src = [[[10, 20], [30, 40]]]
    assert scale_polygons(src, (None, None), (640, 480)) == src
    assert scale_polygons(src, (640, 480), (None, None)) == src
    assert scale_polygons(src, (0, 0), (640, 480)) == src


def test_scale_polygons_빈_입력은_빈_결과():
    from tot_dashboard.common.roi import scale_polygons
    assert scale_polygons([], (200, 100), (400, 200)) == []
    assert scale_polygons(None, (200, 100), (400, 200)) == []
