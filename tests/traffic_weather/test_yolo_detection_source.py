"""Verifies the Phase-3 improvement to YoloDetectionSource: class ids are
resolved BY NAME via common.models_loader.select_class_ids instead of the
original flood3 code's hard-coded COCO id dict (docs/integration_plan.md
section 3-2).
"""
from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.traffic_weather.perception.detection_source import (
    PERSON_CLASSES,
    VEHICLE_CLASSES,
    YoloDetectionSource,
)

SAMPLE_VIDEO = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"
MODEL_PATH = PROJECT_ROOT / "models" / "yolo11s.pt"


def test_target_ids_resolved_by_name_not_hardcoded():
    source = YoloDetectionSource(str(SAMPLE_VIDEO), model=str(MODEL_PATH), fps=5.0)
    resolved_names = set(source._target_id_to_name.values())
    assert resolved_names == (VEHICLE_CLASSES | PERSON_CLASSES)
    # COCO class 0 is "person" in the stock model -- confirm the dynamic
    # resolution landed on the same id the old hard-coded dict used, without
    # us hard-coding it here either (looked up from the loaded model itself).
    assert source._model.names[0] == "person"
    assert 0 in source._target_id_to_name


def test_frames_yields_real_detections_with_resolved_class_names():
    source = YoloDetectionSource(str(SAMPLE_VIDEO), model=str(MODEL_PATH), fps=5.0, loop=False)
    first = next(iter(source.frames()))
    t, dets, frame = first
    assert frame is not None
    for d in dets:
        assert d.cls in (VEHICLE_CLASSES | PERSON_CLASSES)
