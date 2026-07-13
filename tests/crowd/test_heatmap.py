import numpy as np

from tot_dashboard.crowd.heatmap import density_to_heatmap, grid_density, make_grid, overlay_heatmap


def test_make_grid_scales_with_resolution():
    cols, rows, cell = make_grid(1920, 1080, cols=96)
    assert cols == 96
    assert rows > 0
    assert cell == 1920 / 96


def test_grid_density_places_mass_near_boxes():
    W, H = 400, 300
    boxes = [[190, 140, 210, 160]]  # centered-ish ~(200, 160 feet point)
    grid, (cols, rows, cell) = grid_density(boxes, W, H)
    assert grid.shape == (rows, cols)
    assert grid.sum() > 0


def test_empty_boxes_produce_zero_density():
    grid, _ = grid_density([], 200, 200)
    assert grid.sum() == 0


def test_density_to_heatmap_and_overlay_shapes_match():
    W, H = 100, 80
    grid, _ = grid_density([[10, 10, 20, 20]], W, H)
    heat, g_full = density_to_heatmap(grid, W, H)
    assert heat.shape == (H, W, 3)
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    out = overlay_heatmap(frame, heat)
    assert out.shape == frame.shape
