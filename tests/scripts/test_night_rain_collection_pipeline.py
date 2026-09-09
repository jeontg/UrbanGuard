# -*- coding: utf-8 -*-
"""교통 야간·우천 학습 데이터 파이프라인 (2026-08-29, 「4대탐지기능
성능개선 로드맵」 4단계).

``scripts/``는 패키지가 아니라서(``__init__.py`` 없음) 경로로 직접
불러온다 — ``tests/scripts/test_traffic_incident_pipeline.py``와 같은
방식.

지켜야 할 것:

* **야간 판정은 실제 시각을 쓴다** — ``crowd/field_sensors.py::
  MockEnvironmentProvider``와 같은 규칙(20시~06시)
* **KMA 키가 없으면 강수를 "0mm"로 지어내지 않는다** — ``None``(모른다)을
  돌려주고, 호출부는 그 경우 야간 조건만으로 판단한다
* **가라벨 클래스 순서가 고정돼 있다** — 순서가 바뀌면 이미 라벨링한
  파일의 클래스 번호가 전부 어긋난다
* **학습 진입점은 데이터가 없으면 반드시 거부한다**(``train_traffic_
  vehicle_yolo.py``, 기존 관례)
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tot_dashboard.common.config import PROJECT_ROOT

SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _load_script(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_script_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def collect_mod():
    return _load_script("collect_traffic_night_rain_frames")


@pytest.fixture(scope="module")
def label_mod():
    return _load_script("label_traffic_vehicle_auto")


@pytest.fixture(scope="module")
def train_mod():
    return _load_script("train_traffic_vehicle_yolo")


# --- collect_traffic_night_rain_frames.py — 야간·강수 판정 -------------------

@pytest.mark.parametrize("hour,expected", [
    (19, False), (20, True), (23, True), (0, True), (5, True), (6, False), (12, False),
])
def test_야간_판정이_20시_06시_규칙과_같다(collect_mod, hour, expected):
    now = datetime(2026, 8, 29, hour, 0)
    assert collect_mod._is_night(now) is expected


def test_KMA_키가_없으면_강수를_None으로_돌려준다(collect_mod, monkeypatch):
    monkeypatch.delenv("KMA_SERVICE_KEY", raising=False)
    assert collect_mod._check_rain(35.1, 129.0) is None


def test_KMA_조회가_실패해도_None을_돌려준다(collect_mod, monkeypatch):
    """0.0mm(비 안 옴)으로 지어내면 실제로 비가 오는데도 수집을 건너뛸
    수 있다 — 모른다는 것과 안 온다는 것은 다르다."""
    monkeypatch.setenv("KMA_SERVICE_KEY", "dummy")

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("네트워크 실패(시험)")

    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.rainfall_provider.KmaRainfallProvider",
        _Boom)
    assert collect_mod._check_rain(35.1, 129.0) is None


# --- label_traffic_vehicle_auto.py — 가라벨 형식 -----------------------------

def test_클래스_순서가_train_스크립트_문서와_일치한다(label_mod):
    """docstring이 car·bus·truck·motorcycle 순서를 명시한다 — 순서가
    코드와 어긋나면 학습이 엉뚱한 것을 배운다."""
    assert label_mod.CLASS_NAMES == ["car", "bus", "truck", "motorcycle"]


def test_yolo_변환_형식이_다섯_칸이다(label_mod):
    lines = label_mod.boxes_to_yolo_lines([(10, 20, 110, 120)], [2], img_w=200, img_h=200)
    assert len(lines) == 1
    parts = lines[0].split()
    assert len(parts) == 5
    assert parts[0] == "2"   # truck


def test_data_yaml이_이미_있으면_덮어쓰지_않는다(label_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(label_mod, "OUT_ROOT", tmp_path)
    yaml_path = tmp_path / "data.yaml"
    yaml_path.write_text("# 사람이 손으로 고친 파일\n", encoding="utf-8")
    label_mod._write_data_yaml()
    assert yaml_path.read_text(encoding="utf-8") == "# 사람이 손으로 고친 파일\n"


def test_data_yaml이_없으면_클래스_순서대로_생성한다(label_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(label_mod, "OUT_ROOT", tmp_path)
    label_mod._write_data_yaml()
    text = (tmp_path / "data.yaml").read_text(encoding="utf-8")
    assert "0: car" in text
    assert "3: motorcycle" in text


# --- train_traffic_vehicle_yolo.py — 데이터 없으면 거부 ----------------------

def test_데이터가_없으면_실행을_거부한다(train_mod, monkeypatch):
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: None)
    monkeypatch.setattr(sys, "argv", ["train_traffic_vehicle_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    assert "차량검출" in str(exc.value) or "데이터가 없습니다" in str(exc.value)


def test_data_yaml이_없으면_거부한다(train_mod, monkeypatch, tmp_path):
    ds_dir = tmp_path / "traffic_vehicle_own" / "yolo"
    ds_dir.mkdir(parents=True)
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: ds_dir)
    monkeypatch.setattr(sys, "argv", ["train_traffic_vehicle_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    assert "data.yaml" in str(exc.value)


def test_베이스_가중치가_없으면_거부한다(train_mod, monkeypatch, tmp_path):
    ds_dir = tmp_path / "traffic_vehicle_own" / "yolo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "data.yaml").write_text("names: [car, bus, truck, motorcycle]\n",
                                      encoding="utf-8")
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: ds_dir)
    monkeypatch.setattr(train_mod, "BASE_WEIGHTS_DEFAULT", tmp_path / "없는가중치.pt")
    monkeypatch.setattr(sys, "argv", ["train_traffic_vehicle_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    assert "시작 가중치" in str(exc.value)
