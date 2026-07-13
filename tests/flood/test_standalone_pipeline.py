"""End-to-end regression check for flood/standalone_pipeline.py.

Runs the full pipeline (water segmentation + object detection + metrics +
alert + risk + prediction) over the real sample video and real model weights
copied into this repo. This is the closest we can get in an automated test to
underpath_flood_dashboard's original ``scripts/e2e_test.py``/``risk_test.py``
manual checks: crash-free run over every sampled frame, with every derived
value staying in its valid range.
"""
from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.roi import load_roi_config
from tot_dashboard.flood.standalone_pipeline import Pipeline

SAMPLE_VIDEO = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"
ROI_PATH = PROJECT_ROOT / "configs" / "roi" / "UNDERPATH-01.json"


def test_full_pipeline_over_sample_video_stays_in_valid_ranges():
    roi = load_roi_config(ROI_PATH)
    pipeline = Pipeline.from_config(roi=roi)

    source = {"type": "video", "path": str(SAMPLE_VIDEO)}
    from tot_dashboard.common.video_io import iter_source

    frame_count = 0
    for frame_number, ts, frame in iter_source(source, process_every_seconds=1.0):
        result = pipeline.process_frame(frame, frame_number, ts)
        m = result.metrics

        assert 0.0 <= m.water_area_ratio <= 1.0
        assert 1 <= m.alert_level <= 5
        assert 0.0 <= m.risk_score <= 100.0
        assert 1 <= m.risk_grade <= 5
        assert m.risk_trend in ("surge", "rising", "stable", "falling", "insufficient")
        frame_count += 1

    assert frame_count > 0
