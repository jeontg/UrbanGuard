"""모델 레지스트리와 시험 탐지 (core/model_registry.py · core/model_probe.py).

지켜야 할 것.

* **학습해 둔 모델이 화면에서 사라지지 않는다** — 체크포인트를 보관소로 옮겼다고
  후보 목록에서 빠지면 옮긴 의미가 없다
* **중간 산출물은 후보가 아니다** — epoch12.pt 가 수십 개씩 섞이면 못 고른다
* **운영 중인 모델이 무엇인지 구분된다** — 「바꾸면 운영이 바뀐다」와 「시험용
  후보일 뿐이다」를 섞으면 안 된다
* **시험은 운영을 건드리지 않는다** — 결과가 이벤트·노면 현황에 남으면 관제
  화면을 믿을 수 없게 된다
* **동시에 하나만 돈다** — CPU 추론 두 개가 겹치면 둘 다 느려지고 스트림도 놓친다
* **미리보기는 사람을 가린 뒤에만 저장한다** — 가리지 못하면 저장하지 않는다
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.core import model_probe as MP
from tot_dashboard.core import model_registry as MR


# --- 탐색 -------------------------------------------------------------------
def test_알려진_모델은_운영_위치를_함께_알려_준다():
    """「바꾸면 운영이 바뀐다」를 화면이 구분할 수 있어야 한다."""
    known = [m for m in MR.all_models() if m.builtin]
    assert known, "제품이 아는 모델이 하나도 없다"
    assert any(m.in_use for m in known), "운영 중 표시가 하나도 없다"


def test_도메인별로_추린다():
    for dom in ("flood", "traffic", "crowd", "road"):
        for m in MR.for_domain(dom, include_missing=True):
            # 분류 미상은 일부러 함께 준다 — 자동 인식이 틀렸을 때 직접 고를 수 있어야 한다.
            assert m.domain in (dom, ""), f"{dom} 목록에 {m.domain} 모델이 섞였다"


def test_없는_파일은_기본적으로_빼고_준다():
    rows = MR.for_domain("road")
    assert all(m.exists for m in rows)


def test_학습_중간_산출물은_후보에_넣지_않는다(tmp_path, monkeypatch):
    runs = tmp_path / "runs" / "exp" / "weights"
    runs.mkdir(parents=True)
    for name in ("best.pt", "last.pt", "epoch12.pt", "epoch30.pt"):
        (runs / name).write_bytes(b"x")
    monkeypatch.setattr(MR, "_scan_roots", lambda: [tmp_path])

    keys = [m.key for m in MR.all_models(include_missing=False)]
    found = [k for k in keys if "epoch" in k]
    assert not found, f"중간 산출물이 목록에 들어왔다: {found}"


def test_best가_있으면_last는_감춘다(tmp_path, monkeypatch):
    """둘은 같은 학습의 다른 에폭이라, 나란히 놓으면 고를 때 헷갈린다."""
    w = tmp_path / "runs" / "exp" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"x")
    (w / "last.pt").write_bytes(b"x")
    monkeypatch.setattr(MR, "_scan_roots", lambda: [tmp_path])

    keys = [m.key for m in MR.all_models(include_missing=False)]
    assert any(k.endswith("best.pt") for k in keys)
    assert not any(k.endswith("last.pt") for k in keys)


def test_best가_없으면_last라도_보여_준다():
    """학습이 중간에 끊겼을 때 남는 것이 last.pt 뿐인 경우가 있다."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "runs" / "exp" / "weights"
        w.mkdir(parents=True)
        (w / "last.pt").write_bytes(b"x")
        orig = MR._scan_roots
        MR._scan_roots = lambda: [Path(td)]
        try:
            keys = [m.key for m in MR.all_models(include_missing=False)]
        finally:
            MR._scan_roots = orig
    assert any(k.endswith("last.pt") for k in keys)


def test_경로에서_도메인을_추측한다():
    assert MR._guess_domain("data/datasets/svrdd/runs/x/weights/best.pt") == "road"
    assert MR._guess_domain("runs/flood_lraspp_384/best.pt") == "flood"
    assert MR._guess_domain("runs/무엇인지_모를것/best.pt") == ""


def test_범용_YOLO_버전_이름만으로는_도메인을_못_박지_않는다():
    """2026-08-22 전수점검 — "yolo11"·"yolo26" 같은 아키텍처 세대 이름이
    crowd 열쇠말에 들어 있던 탓에, 실제 등록된 범용 검출 모델 5개가 전부
    무조건 crowd 로 확정돼 `for_domain('traffic')` 이 0건이었다. 이제
    이런 이름만으로는 분류 미상("")이어야 하고, `for_domain()` 이 미상
    모델을 모든 도메인에 함께 보여주므로 교통에서도 고를 수 있어야 한다."""
    assert MR._guess_domain("models/yolo11s.pt") == ""
    assert MR._guess_domain("models/candidates/yolo26n.pt") == ""
    assert "models/candidates/yolo26n.pt" in {m.key for m in MR.for_domain("traffic")}


def test_그래도_사람_열쇠말이_있으면_인파로_잡는다():
    assert MR._guess_domain("models/person_detector.pt") == "crowd"
    assert MR._guess_domain("models/vehicle_counter.pt") == "traffic"


def test_낙하물_화재연기_모델도_교통으로_분류된다():
    """Phase 7(2026-08-26) — 낙하물·화재연기는 별도 도메인이 아니라 교통위험
    안의 위험유형이다(core/vocabulary.py, parent="traffic"). 새 모델 파일도
    이 규칙으로 교통 도메인에 자동으로 잡혀야 한다."""
    assert MR._guess_domain(
        "data/datasets/traffic_incident_own/runs/x/weights/best.pt") == "traffic"
    assert MR._guess_domain("models/debris_detector.pt") == "traffic"
    assert MR._guess_domain("models/fire_smoke_yolo.pt") == "traffic"


def test_추측한_모델은_확인이_필요하다고_표시한다(tmp_path, monkeypatch):
    """자동 인식이 틀릴 수 있다는 사실을 화면이 숨기면 안 된다."""
    w = tmp_path / "runs" / "mystery" / "weights"
    w.mkdir(parents=True)
    (w / "best.pt").write_bytes(b"x")
    monkeypatch.setattr(MR, "_scan_roots", lambda: [tmp_path])

    found = [m for m in MR.all_models(include_missing=False)
             if m.key.endswith("mystery/weights/best.pt")]
    assert found and found[0].guessed is True
    assert "확인" in found[0].note


def test_보관소로_옮긴_모델도_찾는다(tmp_path, monkeypatch):
    """체크포인트를 프로젝트 밖으로 옮겼다고 목록에서 빠지면 안 된다."""
    archive = tmp_path / "dev-PoC_DATA" / "03_학습결과" / "svrdd" / "weights"
    archive.mkdir(parents=True)
    (archive / "best.pt").write_bytes(b"x")
    monkeypatch.setenv("URBANGUARD_MODEL_DIRS", str(tmp_path / "dev-PoC_DATA"))

    keys = [m.key for m in MR.all_models(include_missing=False)]
    assert any("svrdd" in k for k in keys)


def test_키로_다시_찾을_수_있다():
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")
    got = MR.get(rows[0].key)
    assert got is not None and got.key == rows[0].key


def test_없는_키는_None():
    assert MR.get("data/does/not/exist.pt") is None


def test_상대경로는_프로젝트_기준으로_푼다():
    assert MR.resolve("models/best.pt") == PROJECT_ROOT / "models" / "best.pt"


def test_절대경로는_그대로_쓴다(tmp_path):
    p = tmp_path / "a.pt"
    assert MR.resolve(str(p)) == p


# --- 시험 탐지 ---------------------------------------------------------------
def test_없는_모델은_분명한_오류를_낸다():
    with pytest.raises(LookupError):
        MP.run("road", "data/nope/nope.pt", "BLOCK-X")


def test_알_수_없는_도메인은_거부한다():
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")
    with pytest.raises(ValueError):
        MP.run("weather", rows[0].key, "BLOCK-X")


def test_다른_도메인_전용_모델_시험탐지는_거부한다():
    """2026-08-22 전수점검 — model_ops.choose() 와 같은 종류의 결함:
    화면 드롭다운은 도메인별로 걸러 보여주지만 서버측 검증이 없어, 폼을
    조작하면 노면 모델로 침수 시험탐지를 돌릴 수 있었다."""
    road_only = [m for m in MR.for_domain("road") if m.domain == "road"]
    if not road_only:
        pytest.skip("이 환경에 도메인이 명확한 노면 모델이 없다")
    with pytest.raises(ValueError):
        MP.run("flood", road_only[0].key, "BLOCK-X")


def test_관측_시간은_상한을_넘지_않는다(monkeypatch):
    """사용자가 큰 값을 넣어도 워커 하나를 오래 붙들면 안 된다."""
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")
    seen = {}

    def fake(model, target_id, kind, *, duration_sec, conf):
        seen["duration"] = duration_sec
        return MP.ProbeResult(domain="road", model_key=model.key,
                              model_label=model.label, backend=model.backend,
                              target_id=target_id, target_name=target_id)

    monkeypatch.setattr(MP, "_probe_road", fake)
    MP.run("road", rows[0].key, "BLOCK-X", duration_sec=9999)
    assert seen["duration"] == MP.MAX_DURATION_SEC


def test_동시에_두_개는_돌지_않는다(monkeypatch):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")

    def fake(model, target_id, kind, *, duration_sec, conf):
        # 안쪽에서 다시 부르면 이미 잠겨 있어야 한다.
        with pytest.raises(RuntimeError):
            MP.run("road", model.key, target_id)
        return MP.ProbeResult(domain="road", model_key=model.key,
                              model_label=model.label, backend=model.backend,
                              target_id=target_id, target_name=target_id)

    monkeypatch.setattr(MP, "_probe_road", fake)
    MP.run("road", rows[0].key, "BLOCK-X")
    assert MP.busy() is False, "잠금이 풀리지 않았다"


def test_실패해도_잠금은_풀린다(monkeypatch):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")

    def boom(*a, **kw):
        raise RuntimeError("추론 폭발")

    monkeypatch.setattr(MP, "_probe_road", boom)
    with pytest.raises(RuntimeError):
        MP.run("road", rows[0].key, "BLOCK-X")
    assert MP.busy() is False


def test_사람을_가리지_못하면_미리보기를_저장하지_않는다(monkeypatch):
    """시험 편의를 위해 개인정보 보호를 양보하지 않는다."""
    from tot_dashboard.core import image_mask
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.UNAVAILABLE, 0))
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    name, note = MP._save_preview(img)
    assert name == ""
    assert "저장하지 않" in note


def test_마스킹이_되면_미리보기를_저장한다(monkeypatch, tmp_path):
    from tot_dashboard.core import image_mask
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.NO_TARGET, 0))
    monkeypatch.setattr(MP, "PREVIEW_DIR", tmp_path)
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    name, note = MP._save_preview(img)
    assert name.endswith(".jpg") and note == ""
    assert (tmp_path / name).is_file()


def test_미리보기는_일정_수만_남긴다(monkeypatch, tmp_path):
    from tot_dashboard.core import image_mask
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.NO_TARGET, 0))
    monkeypatch.setattr(MP, "PREVIEW_DIR", tmp_path)
    monkeypatch.setattr(MP, "PREVIEW_KEEP", 3)
    img = np.zeros((16, 16, 3), dtype=np.uint8)
    for _ in range(6):
        MP._save_preview(img)
    assert len(list(tmp_path.glob("*.jpg"))) <= 3


def test_프레임을_못_받으면_사유를_알려_준다():
    frames, why = MP._grab_frames("no://such/stream", duration_sec=1.0,
                                  interval_sec=0.5, max_frames=2)
    assert frames == []
    assert why, "실패 사유가 비어 있다"
