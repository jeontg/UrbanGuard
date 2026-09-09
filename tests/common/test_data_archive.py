"""학습 데이터 보관소 경로 해석(``common/data_archive.py``).

지켜야 할 것.

* **프로젝트 경로가 있으면(비어있지 않으면) 그걸 먼저 쓴다** — 보관소보다 우선
* **둘 다 없거나 비어 있으면 None** — 있는 척하지 않는다
* **모르는 키는 조용히 넘어가지 않는다** — 오타 하나로 엉뚱한 경로를 읽으면
  안 되므로 예외를 낸다
"""
from __future__ import annotations

import pytest

from tot_dashboard.common import data_archive as DA


def test_모르는_키는_예외를_낸다():
    with pytest.raises(KeyError):
        DA.resolve_dataset("없는키")
    with pytest.raises(KeyError):
        DA.dataset_hint("없는키")


def test_둘_다_없으면_None(tmp_path, monkeypatch):
    monkeypatch.setattr(DA, "PROJECT_ROOT", tmp_path / "proj")
    monkeypatch.setattr(DA, "DATA_ARCHIVE_ROOT", tmp_path / "archive")
    assert DA.resolve_dataset("traffic_incident_yolo") is None


def test_프로젝트_경로가_있으면_그걸_우선한다(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    archive = tmp_path / "archive"
    monkeypatch.setattr(DA, "PROJECT_ROOT", proj)
    monkeypatch.setattr(DA, "DATA_ARCHIVE_ROOT", archive)

    proj_ds = proj / "data" / "datasets" / "traffic_incident_own" / "yolo"
    proj_ds.mkdir(parents=True)
    (proj_ds / "data.yaml").write_text("x", encoding="utf-8")
    archive_ds = archive / "07_학습데이터_교통" / "traffic_incident_own" / "yolo"
    archive_ds.mkdir(parents=True)
    (archive_ds / "data.yaml").write_text("x", encoding="utf-8")

    assert DA.resolve_dataset("traffic_incident_yolo") == proj_ds


def test_프로젝트_경로가_비어있으면_보관소로_물러난다(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    archive = tmp_path / "archive"
    monkeypatch.setattr(DA, "PROJECT_ROOT", proj)
    monkeypatch.setattr(DA, "DATA_ARCHIVE_ROOT", archive)

    proj_ds = proj / "data" / "datasets" / "traffic_incident_own" / "yolo"
    proj_ds.mkdir(parents=True)  # 빈 폴더 — 있어도 안 쓴다
    archive_ds = archive / "07_학습데이터_교통" / "traffic_incident_own" / "yolo"
    archive_ds.mkdir(parents=True)
    (archive_ds / "data.yaml").write_text("x", encoding="utf-8")

    assert DA.resolve_dataset("traffic_incident_yolo") == archive_ds


def test_안내문에_두_경로가_모두_나온다(tmp_path, monkeypatch):
    monkeypatch.setattr(DA, "DATA_ARCHIVE_ROOT", tmp_path / "archive")
    hint = DA.dataset_hint("traffic_incident_yolo")
    assert "data/datasets/traffic_incident_own/yolo" in hint
    assert "07_학습데이터_교통" in hint


# --- 낙하물·화재연기 키 등록 확인 (Phase 7, 2026-08-26) -----------------------


def test_traffic_incident_yolo_키가_등록돼_있다():
    """07_학습데이터_교통 아래 traffic_vehicle_yolo와 같은 구조의 별도
    서브셋 — 낙하물·화재연기는 별도 도메인이 아니므로 새 번호를 안 쓴다."""
    assert "traffic_incident_yolo" in DA._DATASET_MAP
    proj_rel, archive_rel = DA._DATASET_MAP["traffic_incident_yolo"]
    assert proj_rel == "data/datasets/traffic_incident_own/yolo"
    assert archive_rel == "07_학습데이터_교통/traffic_incident_own/yolo"
