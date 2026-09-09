"""물 세그멘테이션 모델 공유 (service/runner.py).

지금까지 ``_load_water_model()`` 이 **블록마다 호출**돼 같은 가중치를 지점
수만큼 메모리에 올렸다. 3지점이면 3벌이다.

지켜야 할 것.

* **지점이 몇 개든 모델은 하나** — 가중치는 무상태라 공유해도 결과가 같다
* **실패도 기억한다** — 안 그러면 블록마다 다시 시도하며 같은 오류를 지점
  수만큼 찍는다
* ⚠️ **분석기를 공유하는 것과 다르다** — 분석기는 추적 ID·배회 타이머를
  들고 있어 공유하면 지점 간에 뒤섞인다. 여기서 공유하는 것은 **가중치뿐**
"""
from __future__ import annotations

import pytest

from tot_dashboard.service.runner import PipelineRunner


class _Fake(PipelineRunner):
    """실제 모델을 올리지 않고 호출 횟수만 센다."""

    def __init__(self):
        # PipelineRunner.__init__ 은 블록·스트림을 잡으므로 우회한다.
        self._water_model_shared = None
        self._water_model_cached = False
        self._water_backend = "ultralytics"
        self.built = 0
        self._result = object()

    def _build_water_model(self):
        self.built += 1
        return self._result


def test_여러_번_불러도_한_번만_만든다():
    r = _Fake()
    a = r._load_water_model()
    b = r._load_water_model()
    c = r._load_water_model()
    assert r.built == 1, f"모델을 {r.built}번 만들었다 — 지점마다 만들면 안 된다"
    assert a is b is c, "같은 인스턴스를 돌려줘야 공유가 의미 있다"


def test_실패도_기억한다():
    """안 그러면 블록마다 다시 시도하며 같은 오류를 지점 수만큼 찍는다."""
    r = _Fake()
    r._result = None
    assert r._load_water_model() is None
    assert r._load_water_model() is None
    assert r.built == 1, f"실패를 {r.built}번 재시도했다"


def test_처음에는_비어_있다():
    r = _Fake()
    assert r._water_model_cached is False
    assert r._water_model_shared is None


def test_공유_대상은_가중치뿐이다():
    """분석기 공유와 혼동하지 않기 위한 표식.

    인파 분석기는 **카메라마다** 있어야 한다(추적 ID·배회 타이머). 이
    시험이 깨지면 누군가 공유 범위를 넓힌 것이므로 그 근거를 확인해야 한다.
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
           / "service" / "continuous.py").read_text(encoding="utf-8")
    assert "카메라마다 별도 분석기를 둔다" in src, (
        "인파 분석기를 공유하면 추적 ID 와 배회 타이머가 지점 간에 뒤섞인다")
