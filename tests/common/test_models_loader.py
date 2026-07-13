"""Loads the two real YOLO weight files copied into models/ (Phase 1 completion
criterion). These are ~20 MB and ~19 MB respectively so this test is slower
than the rest of the suite but is the actual regression check that matters:
underpath_flood_dashboard's model_loader.py must still load both weight files
and resolve person/vehicle class ids by name after the move.
"""
from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.models_loader import load_models, select_class_ids

MODELS_DIR = PROJECT_ROOT / "models"


def test_load_water_segmentation_model():
    from tot_dashboard.common.models_loader import load_model

    model = load_model(MODELS_DIR / "best.pt", device="cpu")
    names = dict(model.names)
    assert any("water" in str(n).lower() for n in names.values())


def test_load_object_model_and_resolve_classes_by_name():
    from tot_dashboard.common.models_loader import load_model

    model = load_model(MODELS_DIR / "yolo11s.pt", device="cpu")
    person_ids = select_class_ids(model, ["person"])
    vehicle_ids = select_class_ids(model, ["car", "bus", "truck", "motorcycle"])
    assert person_ids == [0]  # COCO class 0 = person
    assert len(vehicle_ids) == 4


def test_load_models_bundle():
    bundle = load_models(
        {
            "water_model_path": str(MODELS_DIR / "best.pt"),
            "object_model_path": str(MODELS_DIR / "yolo11s.pt"),
            "device": "cpu",
            "person_classes": ["person"],
            "vehicle_classes": ["car", "bus", "truck", "motorcycle"],
        }
    )
    assert bundle.person_class_ids == [0]
    assert set(bundle.target_class_ids) >= {0}
