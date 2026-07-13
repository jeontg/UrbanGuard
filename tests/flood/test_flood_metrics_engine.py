"""Regression check for flood_metrics_engine.py, mirroring flood3's original
``scripts/smoke_test_flood.py`` (water segmentation -> FloodMetricsEngine ->
RiskEngine -> RiskPredictor, crash-free, water_area_ratio in [0, 1]).
"""
import cv2

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.models_loader import load_model
from tot_dashboard.common.roi import load_roi_config
from tot_dashboard.common.video_io import read_first_frame
from tot_dashboard.flood.flood_metrics_engine import FloodMetricsEngine
from tot_dashboard.flood.risk_engine import RiskEngine, RiskPredictor, write_back
from tot_dashboard.flood.water_segmentation import segment_water
from tot_dashboard.models import TrafficMetrics, TrafficState

SAMPLE_VIDEO = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"
ROI_PATH = PROJECT_ROOT / "configs" / "roi" / "UNDERPATH-01.json"
MODEL_PATH = PROJECT_ROOT / "models" / "best.pt"


def test_flood_metrics_engine_end_to_end_single_frame():
    frame = read_first_frame(SAMPLE_VIDEO)
    assert frame is not None

    model = load_model(MODEL_PATH, device="cpu")
    water = segment_water(model, frame, conf=0.10, iou=0.50, imgsz=640, device="cpu")

    roi = load_roi_config(ROI_PATH)
    engine = FloodMetricsEngine(roi=roi)
    traffic = TrafficMetrics(
        t_sec=0.0, n_vehicles=0, mean_speed=0.0, speed_drop=0.0,
        density=0.0, queue_len=0, stalled=0, state=TrafficState.free,
    )
    m = engine.update(water, vehicles=[], person_points=[], traffic=traffic,
                      frame_number=0, timestamp_sec=0.0)
    assert 0.0 <= m.water_area_ratio <= 1.0
    assert m.roi_defined is True

    risk_engine = RiskEngine({}, {})
    risk = risk_engine.score(m)
    m.risk_score, m.risk_grade = risk.risk_score, risk.risk_grade
    predictor = RiskPredictor(risk_engine, {})
    pred = predictor.update(m)
    write_back(m, risk, pred)

    assert 0.0 <= m.risk_score <= 100.0
    assert 1 <= m.risk_grade <= 5
    # a single frame is not enough history -> predictor must report "insufficient"
    assert pred.trend_label == "insufficient"
