# -*- coding: utf-8 -*-
"""``_retry_restream_if_due()`` — 재배포(MediaMTX) 재연결 재시도.

2026-08-29 실사용 중 발견 — MediaMTX 프로세스가 재부팅 후 자동으로 안
살아나는 결함(``scripts/urbanguard-run.cmd`` 참고)으로 재배포가 몇
시간이고 죽어 있었는데, 그동안 살아 있던 서비스 프로세스는 **재기동
전까지 계속 "관측 없음"에 머물렀다** — ``_build_source()``가 지점 구성
시점에 딱 한 번만 불려 그 결과(``kind``)가 프로세스 수명 내내 고정되기
때문이다. 이 시험은 주기적 재시도가 그 고착을 스스로 풀어 주는지
고정한다.

지켜야 할 것:

* kind가 ``restream_unavailable``이 아니면 아무 것도 안 한다(불필요한
  재연결 시도로 자원을 낭비하지 않는다)
* 재시도 간격(``RESTREAM_RETRY_SEC``)을 채우기 전에는 다시 시도하지
  않는다 — 매 틱 재시도하면 회복 안 된 상태에서 접속 시도 로그만 쌓인다
* 재시도가 **또 실패**하면 아무것도 바꾸지 않고 다음 주기로 넘어간다
* 재시도가 **성공**하면 ``frames``·``wh``·``kind``·``perception``·
  ``water_model``을 전부 새 소스 기준으로 바꾼다 — 하나라도 안 바꾸면
  옛 상태와 뒤섞인다
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tot_dashboard.service.runner import PipelineRunner


def _make_runner() -> PipelineRunner:
    r = object.__new__(PipelineRunner)
    r.fps = 5.0
    return r


class _RaisingYolo:
    def __init__(self, *a, **k):
        raise RuntimeError("연결 거부됨(시험)")


class _OkYolo:
    def __init__(self, *a, **k):
        self.frame_wh = (640, 360)
        self.base_speed = 1.0

    def frames(self):
        yield (0.0, [], None)


def _restream_block(bid="TEST-RESTREAM-RETRY"):
    return {"id": bid, "name": "시험지점", "source": {
        "type": "hls", "url": f"rtsp://127.0.0.1:8554/{bid}",
        "origin_url": "https://example.test/original.m3u8",
    }}


def _make_ctx(kind="restream_unavailable", next_retry_t=0.0):
    return {
        "block": _restream_block(), "rainfall": SimpleNamespace(),
        "kind": kind, "frames": iter([]), "wh": (1, 1),
        "next_restream_retry_t": next_retry_t,
    }


def test_restream_unavailable가_아니면_아무_것도_안_한다(monkeypatch):
    runner = _make_runner()
    calls = []
    monkeypatch.setattr(runner, "_build_source",
                        lambda *a, **k: calls.append(1))
    c = _make_ctx(kind="hls")
    runner._retry_restream_if_due(c)
    assert calls == [], "정상 연결 중인데 재연결을 시도했다"


def test_재시도_간격을_채우기_전에는_시도하지_않는다(monkeypatch):
    runner = _make_runner()
    calls = []
    monkeypatch.setattr(runner, "_build_source",
                        lambda *a, **k: (calls.append(1), None, None, "restream_unavailable")[1:])
    monkeypatch.setattr("tot_dashboard.service.runner.time.time", lambda: 1000.0)
    c = _make_ctx(kind="restream_unavailable", next_retry_t=2000.0)  # 아직 안 됨
    runner._retry_restream_if_due(c)
    assert calls == [], "재시도 간격 전인데 재연결을 시도했다"


def test_간격이_지나면_재시도하고_또_실패하면_kind는_그대로다(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _RaisingYolo)
    monkeypatch.setattr("tot_dashboard.service.runner.time.time", lambda: 1000.0)
    c = _make_ctx(kind="restream_unavailable", next_retry_t=500.0)  # 이미 지남
    old_frames = c["frames"]
    runner._retry_restream_if_due(c)
    assert c["kind"] == "restream_unavailable", "재시도도 실패했는데 kind가 바뀌었다"
    assert c["frames"] is old_frames, "실패했는데 frames가 바뀌었다"
    assert c["next_restream_retry_t"] == pytest.approx(1000.0 + runner.RESTREAM_RETRY_SEC), (
        "재시도 후 다음 재시도 시각이 갱신되지 않았다 — 매 틱 재시도하게 된다")


def test_간격이_지나_재시도가_성공하면_상태가_전부_바뀐다(monkeypatch):
    runner = _make_runner()
    runner._load_water_model = lambda: "WATER_MODEL_LOADED"
    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _OkYolo)
    monkeypatch.setattr(
        "tot_dashboard.core.model_ops.selected_key", lambda domain: "")
    monkeypatch.setattr("tot_dashboard.service.runner.time.time", lambda: 1000.0)
    c = _make_ctx(kind="restream_unavailable", next_retry_t=500.0)

    runner._retry_restream_if_due(c)

    assert c["kind"] == "hls", "재연결에 성공했는데 kind가 그대로다"
    assert c["wh"] == (640, 360)
    assert c["water_model"] == "WATER_MODEL_LOADED", "물 세그멘테이션 모델을 다시 안 실었다"
    assert c["perception"] is not None
    # frames는 제너레이터라 객체 동일성 대신 실제로 값을 낼 수 있는지 확인.
    t, dets, frame = next(c["frames"])
    assert t == 0.0


def test_회복_후에는_다시_restream_unavailable로_불려도_또_시도한다(monkeypatch):
    """재시도가 성공한 뒤 나중에 다시 끊기면(예: 다른 카메라 문제), 그때도
    같은 재시도 체계를 그대로 탄다 — 별도 처리가 필요 없다(kind가
    다시 restream_unavailable로 떨어지면 이 함수가 다시 반응한다)."""
    runner = _make_runner()
    calls = []
    monkeypatch.setattr(runner, "_build_source",
                        lambda *a, **k: (calls.append(1), (None, None, "restream_unavailable"))[1])
    monkeypatch.setattr("tot_dashboard.service.runner.time.time", lambda: 5000.0)
    c = _make_ctx(kind="restream_unavailable", next_retry_t=0.0)
    runner._retry_restream_if_due(c)
    assert len(calls) == 1
