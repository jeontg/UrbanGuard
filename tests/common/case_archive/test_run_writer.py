"""Verify common/case_archive/run_writer.py by actually running the flood
standalone pipeline (Phase 2) over the real sample video with archiving
enabled — this is the "flood run archived via the RunWriter mechanism" path
described in docs/integration_plan.md Phase 6.
"""
import functools

from tot_dashboard.common.case_archive.run_writer import RunWriter, list_runs, load_run
from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.roi import load_roi_config
from tot_dashboard.flood.standalone_pipeline import Pipeline, run_to_completion

SAMPLE_VIDEO = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"
ROI_PATH = PROJECT_ROOT / "configs" / "roi" / "UNDERPATH-01.json"


def test_flood_pipeline_run_is_archived_and_reloadable(tmp_path):
    roi = load_roi_config(ROI_PATH)
    pipeline = Pipeline.from_config(roi=roi)
    source = {"type": "video", "path": str(SAMPLE_VIDEO)}

    writer_factory = functools.partial(RunWriter, runs_dir=tmp_path)
    summary = run_to_completion(pipeline, source, writer_factory=writer_factory)

    assert summary["frames"] > 0
    assert 1 <= summary["max_alert_level"] <= 5

    runs = list_runs(runs_dir=tmp_path)
    assert len(runs) == 1
    assert runs[0].frames == summary["frames"]

    loaded = load_run(runs[0].run_dir)
    assert len(loaded["metrics"]) == summary["frames"]
    assert loaded["config"]["roi_camera"] == roi.camera_name
