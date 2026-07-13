"""Shared config/path helpers.

Merges underpath_flood_dashboard's ``src/config_utils.py`` with flood3's
``_load_yaml()`` helper, which was duplicated byte-for-byte in
``service/runner.py`` and ``scripts/smoke_test_flood.py`` (confirmed during the
integration review — see docs/integration_plan.md section 2). The two projects'
merge strategies differ slightly: underpath_flood_dashboard does a flat
``defaults.update(loaded)``, while flood3's duplicated helper special-cases a
nested ``weights`` dict so it merges instead of being replaced wholesale. Both
are exposed here so callers can pick the one that matches their config's shape.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import yaml

# Repo root = four levels up from this file (common/ -> tot_dashboard/ -> src/ -> repo root).
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def resolve_path(p: str | "os.PathLike[str]" | None) -> Path | None:
    """Resolve a possibly-relative path against the project root."""
    if p is None or str(p).strip() == "":
        return None
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_yaml(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def load_config_simple(path: str | Path | None, defaults: dict[str, Any]) -> dict[str, Any]:
    """underpath_flood_dashboard style: ``defaults`` overlaid with a flat
    ``defaults.update(loaded)`` if the file exists; missing file -> defaults as-is.
    """
    merged = dict(defaults)
    resolved = resolve_path(path)
    if resolved is not None and resolved.exists():
        merged.update(load_yaml(resolved))
    return merged


def load_config_with_nested_merge(
    path: str | Path | None,
    defaults: dict[str, Any],
    nested_keys: Iterable[str] = ("weights",),
) -> dict[str, Any]:
    """flood3 style (previously duplicated in ``service/runner.py`` and
    ``scripts/smoke_test_flood.py``): like :func:`load_config_simple`, but any
    key named in ``nested_keys`` is dict-merged against the corresponding
    default sub-dict instead of replacing it outright. Used for
    ``risk_config.yaml``'s ``weights`` block so a config file only overriding
    one weight doesn't silently zero out the others.
    """
    merged = dict(defaults)
    resolved = resolve_path(path)
    if resolved is None or not resolved.exists():
        return merged
    loaded = load_yaml(resolved)
    for key in nested_keys:
        if isinstance(loaded.get(key), dict):
            merged[key] = {**defaults.get(key, {}), **loaded.pop(key)}
    merged.update(loaded)
    return merged
