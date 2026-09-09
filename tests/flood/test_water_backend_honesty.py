"""침수 모델을 **실제로 열었는지**, 그리고 화면이 그것을 그대로 말하는지.

## 왜 이 시험이 있나 (2026-08-19 발견)

백엔드(로더)와 모델 파일이 **따로** 정해지는 구조였다. 백엔드는 설정
파일에서, 파일은 S-61 화면에서 정해진다. 둘이 어긋나자 적재가 조용히
실패했다.

운영 중인 서비스에서 실제로 이랬다.

    "backend": "ultralytics",
    "license": "AGPL-3.0 (Ultralytics)",
    "model_path": ".../flood_lraspp_384_v2/best.pt",   ← torchvision 파일
    "models_loaded": 0,                                 ← 아무것도 안 열림
    "blocks": 3

⚠️ **3개 지점 전부에서 침수 세그멘테이션이 안 돌고 있었다.** 그런데 화면에는
모델 경로가 떠 있었고 라이선스 표기까지 **틀렸다**. 맑은 날에는 어느 쪽이든
탐지 0이라 **눈으로는 구분되지 않는다.**

★ 이 시험이 지키는 것은 두 가지다.
   ① 요청한 백엔드가 아니라 **실제로 열린 백엔드**를 보고하는가
   ② 못 열었을 때 **못 열었다고 말하는가**
"""
from __future__ import annotations

from pathlib import Path

import pytest

import threading

from tot_dashboard.service import runner as RN


def _bare_runner(tmp_path: Path, *, backend: str = "auto",
                 model_path: Path | None = None):
    """적재 경로만 떼어 만든 러너. 파이프라인 전체를 띄우지 않는다."""
    r = RN.PipelineRunner.__new__(RN.PipelineRunner)
    r.water_cfg = dict(RN._WATER_DEFAULTS)
    r._water_device = "cpu"
    r._water_model_path = model_path or (tmp_path / "nope.pt")
    r._water_backend = backend
    r._water_backend_effective = ""
    r._water_model_used = None
    r._water_load_error = ""
    r._water_tv_model_path = model_path or (tmp_path / "nope.pt")
    r._water_tv_conf = 0.5
    r._ctx = {}
    # ``__init__`` 을 건너뛰므로 거기서 만들어지는 것을 직접 채운다.
    # ``water_backend_status()`` 가 _ctx 를 스냅샷 뜰 때 쓴다(2026-08-22
    # 동적 재구성으로 추가 — 순회 중 크기 변경 방지).
    r._ctx_lock = threading.RLock()
    return r


def test_torchvision_체크포인트를_알아본다(tmp_path):
    """``{"arch": ..., "model": <state_dict>}`` 꼴이면 torchvision 이다."""
    torch = pytest.importorskip("torch")
    p = tmp_path / "tv.pt"
    torch.save({"arch": "lraspp", "model": {"w": torch.zeros(1)}}, p)
    r = _bare_runner(tmp_path)
    assert r._detect_water_backend(p) == "torchvision"


def test_그_외에는_ultralytics_로_본다(tmp_path):
    """⚠️ 판별에 실패해도 기존 동작으로 떨어진다 — 바꿀 이유가 없다."""
    torch = pytest.importorskip("torch")
    p = tmp_path / "ul.pt"
    torch.save({"model": "이건 state_dict 가 아니다"}, p)
    r = _bare_runner(tmp_path)
    assert r._detect_water_backend(p) == "ultralytics"


def test_읽을_수_없는_파일도_죽지_않는다(tmp_path):
    p = tmp_path / "broken.pt"
    p.write_bytes(b"not a checkpoint")
    r = _bare_runner(tmp_path)
    assert r._detect_water_backend(p) == "ultralytics"


def test_못_열면_못_열었다고_말한다(tmp_path):
    """★ 이것이 이 시험 묶음의 핵심이다.

    예전에는 적재가 실패해도 요청한 백엔드와 라이선스가 그대로 떠 있었다.
    **아무것도 안 도는데 도는 것처럼 보였다.**
    """
    p = tmp_path / "broken.pt"
    p.write_bytes(b"not a checkpoint")
    r = _bare_runner(tmp_path, model_path=p)

    assert r._build_water_model() is None

    st = r.water_backend_status()
    assert st["ok"] is False
    assert st["effective_backend"] == "(적재 실패)"
    # ⚠️ 라이선스를 아는 척하지 않는다 — 납품 검토에 그대로 들어가는 값이다.
    assert "확인 불가" in st["license"]
    assert st["models_loaded"] == 0
    assert "error" in st
    # 탐지 0 을 「침수 없음」으로 읽지 않게 한 문장 덧붙인다.
    assert "탐지 0건이" in st["warning"]


def test_라이선스는_실제로_열린_쪽을_따른다(tmp_path):
    """⚠️ torchvision 파일을 AGPL 로 표기하던 결함을 막는다."""
    r = _bare_runner(tmp_path)
    r._water_backend_effective = "torchvision"
    r._ctx = {"a": {"water_model": object(), "block": {"flood_enabled": True}}}
    assert r.water_backend_status()["license"].startswith("BSD")

    r._water_backend_effective = "ultralytics"
    assert "AGPL" in r.water_backend_status()["license"]


def test_열렸어도_지점에_안_붙었으면_ok_가_아니다(tmp_path):
    """모델을 열었는데 어느 지점에도 안 붙었으면 관제는 여전히 안 된다."""
    r = _bare_runner(tmp_path)
    r._water_backend_effective = "torchvision"
    r._ctx = {"a": {"water_model": None, "block": {"flood_enabled": True}}}
    assert r.water_backend_status()["ok"] is False


def test_기본_설정은_auto_다(tmp_path):
    """★ 사람이 두 곳(백엔드·파일)을 맞춰 둬야 하는 구조를 없앤 것이 요점이다."""
    assert RN._WATER_DEFAULTS["water_backend"] == "auto"
