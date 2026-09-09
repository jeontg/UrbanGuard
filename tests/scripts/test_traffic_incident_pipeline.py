"""낙하물·화재연기 데이터 파이프라인 스크립트 (Phase 7, 2026-08-26).

``scripts/`` 는 패키지가 아니라서(``__init__.py`` 없음) 경로로 직접
불러온다. 지켜야 할 것.

* **라벨 저장→로드가 왕복 일치한다** — ``label_traffic_incident.py``의
  YOLO 포맷 변환이 정확해야 라벨링 도중 실수로 잃는 것이 없다
* **학습 진입점은 데이터가 없으면 반드시 거부한다** — 이게 Phase 7의
  핵심 산출물이다("데이터 없이 「학습했다」는 결과를 내면 안 된다")
* **베이스 가중치·data.yaml 이 없어도 각각 거부한다** — 데이터 폴더만
  있고 속은 비어 있는 상태를 "준비됨"으로 착각하면 안 된다
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tot_dashboard.common.config import PROJECT_ROOT

SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _load_script(name: str):
    """scripts/<name>.py 를 모듈로 불러온다(scripts/ 는 패키지가 아니다)."""
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_script_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def label_mod():
    return _load_script("label_traffic_incident")


@pytest.fixture(scope="module")
def train_mod():
    return _load_script("train_traffic_incident_yolo")


# --- label_traffic_incident.py — YOLO 포맷 왕복 ------------------------------


def test_yolo_line_형식이_다섯_칸이다(label_mod):
    line = label_mod.yolo_line(0, (10, 20, 110, 120), w=200, h=200)
    parts = line.split()
    assert len(parts) == 5
    assert parts[0] == "0"


def test_클래스_이름이_어휘와_일치한다(label_mod):
    """core/vocabulary.py의 traffic_debris·traffic_fire_smoke와 개념이
    같은 두 위험유형이다."""
    assert label_mod.CLASS_NAMES == {0: "debris", 1: "fire_smoke"}


def test_저장하고_다시_읽으면_같은_박스가_나온다(label_mod, tmp_path):
    w, h = 400, 300
    boxes = [(0, (10, 20, 110, 220)), (1, (200, 50, 390, 290))]
    lines = [label_mod.yolo_line(cid, box, w, h) for cid, box in boxes]
    label_path = tmp_path / "frame_1.txt"
    label_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = label_mod.load_existing_labels(label_path, w, h)
    assert len(loaded) == 2
    for (exp_cid, exp_box), (got_cid, got_box) in zip(boxes, loaded):
        assert got_cid == exp_cid
        # 정규화(부동소수)-역정규화(정수 픽셀) 왕복이라 1px 오차는 허용한다.
        assert all(abs(a - b) <= 1 for a, b in zip(exp_box, got_box))


def test_없는_라벨_파일은_빈_목록(label_mod, tmp_path):
    assert label_mod.load_existing_labels(tmp_path / "없음.txt", 100, 100) == []


def test_형식이_깨진_줄은_건너뛴다(label_mod, tmp_path):
    p = tmp_path / "broken.txt"
    p.write_text("0 0.1 0.1\n0 0.5 0.5 0.1 0.1\n", encoding="utf-8")  # 첫 줄은 칸이 모자람
    loaded = label_mod.load_existing_labels(p, 100, 100)
    assert len(loaded) == 1


# --- train_traffic_incident_yolo.py — 데이터 없으면 거부 ----------------------


def test_데이터가_없으면_실행을_거부한다(train_mod, monkeypatch):
    """★ Phase 7 핵심 — 데이터 없이 「학습했다」는 결과를 내면 안 된다."""
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: None)
    monkeypatch.setattr(sys, "argv", ["train_traffic_incident_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    assert "낙하물" in str(exc.value) or "화재" in str(exc.value)


def test_데이터_폴더는_있는데_data_yaml이_없으면_거부한다(train_mod, monkeypatch, tmp_path):
    ds_dir = tmp_path / "traffic_incident_own" / "yolo"
    ds_dir.mkdir(parents=True)
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: ds_dir)
    monkeypatch.setattr(sys, "argv", ["train_traffic_incident_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    assert "data.yaml" in str(exc.value)


def test_data_yaml은_있는데_베이스_가중치가_없으면_거부한다(train_mod, monkeypatch, tmp_path):
    ds_dir = tmp_path / "traffic_incident_own" / "yolo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "data.yaml").write_text("names: [debris, fire_smoke]\n", encoding="utf-8")
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: ds_dir)
    monkeypatch.setattr(train_mod, "BASE_WEIGHTS_DEFAULT", tmp_path / "없는가중치.pt")
    monkeypatch.setattr(sys, "argv", ["train_traffic_incident_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    assert "시작 가중치" in str(exc.value)


def test_안내문에_수집_라벨링_스크립트_이름이_들어있다(train_mod, monkeypatch):
    """데이터가 없다는 오류만 던지고 끝나면 사람이 다음에 뭘 해야 할지
    모른다 — 파이프라인의 다음 단계를 안내해야 한다."""
    monkeypatch.setattr(train_mod, "resolve_dataset", lambda key: None)
    monkeypatch.setattr(sys, "argv", ["train_traffic_incident_yolo.py"])
    with pytest.raises(SystemExit) as exc:
        train_mod.main()
    msg = str(exc.value)
    assert "collect_traffic_incident_frames.py" in msg
    assert "label_traffic_incident.py" in msg
