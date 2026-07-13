"""Load YOLO models and resolve target classes by NAME, not hard-coded id.

Ported verbatim from underpath_flood_dashboard's ``src/model_loader.py``
(confirmed during the integration review to be fully generic and not tied to
any one domain's detection flow). Design rule: never hard-code class ids —
read ``model.names`` and select classes by name so the code keeps working if
the object model's class order ever changes. This is the pattern flood3's
``perception/detection_source.py`` should also be moved onto (see Phase 3 —
that file currently hard-codes COCO ids instead).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import resolve_path


def resolve_device(device: Any) -> Any:
    """Map a config device value to something ultralytics/torch accepts.

    ``auto`` -> GPU 0 if CUDA is available, otherwise ``cpu``.
    Anything else (``cpu``, ``0``, ``"0,1"`` ...) is passed through.
    """
    if device is None or str(device).lower() == "auto":
        try:
            import torch

            return 0 if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"
    return device


def load_model(path: str | Path, device: Any = "cpu"):
    """Load a single ultralytics YOLO model and move it to ``device``."""
    from ultralytics import YOLO

    resolved = resolve_path(path)
    if resolved is None or not resolved.exists():
        raise FileNotFoundError(f"Model file not found: {path} (resolved: {resolved})")
    model = YOLO(str(resolved))
    try:
        model.to(resolve_device(device))
    except Exception:
        # Some exports don't support .to(); inference-time device= still applies.
        pass
    return model


def select_class_ids(model, wanted_names: list[str]) -> list[int]:
    """Return class ids in ``model.names`` whose name matches ``wanted_names``.

    Matching is case-insensitive. Names not present in the model are skipped
    (so an optional class like ``bicycle`` simply has no effect if absent).
    """
    names: dict[int, str] = dict(model.names)
    wanted = {w.strip().lower() for w in wanted_names}
    return sorted(cid for cid, nm in names.items() if str(nm).strip().lower() in wanted)


@dataclass
class ModelBundle:
    """Both models plus resolved class-id selections and metadata."""

    water_model: Any
    object_model: Any
    device: Any
    water_model_path: str
    object_model_path: str
    object_names: dict[int, str] = field(default_factory=dict)
    person_class_ids: list[int] = field(default_factory=list)
    vehicle_class_ids: list[int] = field(default_factory=list)

    @property
    def target_class_ids(self) -> list[int]:
        return sorted(set(self.person_class_ids) | set(self.vehicle_class_ids))

    def summary(self) -> dict[str, Any]:
        names = self.object_names
        return {
            "device": str(self.device),
            "water_model_path": self.water_model_path,
            "object_model_path": self.object_model_path,
            "object_num_classes": len(names),
            "person_classes": [names.get(i, "?") for i in self.person_class_ids],
            "vehicle_classes": [names.get(i, "?") for i in self.vehicle_class_ids],
        }


def load_models(config: dict[str, Any]) -> ModelBundle:
    """Load both models from a model-config dict and resolve class ids by name."""
    device = resolve_device(config.get("device", "auto"))

    water_model = load_model(config["water_model_path"], device)
    object_model = load_model(config["object_model_path"], device)

    object_names = dict(object_model.names)
    person_ids = select_class_ids(object_model, config.get("person_classes", ["person"]))
    vehicle_ids = select_class_ids(
        object_model, config.get("vehicle_classes", ["car", "bus", "truck", "motorcycle"])
    )

    return ModelBundle(
        water_model=water_model,
        object_model=object_model,
        device=device,
        water_model_path=str(config["water_model_path"]),
        object_model_path=str(config["object_model_path"]),
        object_names=object_names,
        person_class_ids=person_ids,
        vehicle_class_ids=vehicle_ids,
    )


def select_class_ids_by_model_path(model_path: str | Path, wanted_names: list[str],
                                    device: Any = "cpu") -> list[int]:
    """Convenience one-shot: load a model just to resolve class ids.

    Useful for callers (e.g. flood3's YoloDetectionSource, Phase 3) that only
    need the id list once at startup rather than the full ModelBundle.
    """
    model = load_model(model_path, device)
    return select_class_ids(model, wanted_names)
