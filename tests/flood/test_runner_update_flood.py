"""회귀 — ``PipelineRunner._update_flood`` 이 실제로 위험 계산을 끝까지 수행한다
(S-88, 2026-08-20).

## 왜 이 시험이 있나

침수 지점의 탐지 상자(``push_boxes``)를 넣으며

```python
_evidence.push_boxes(bid, boxes, (fw, fh))
```

를 썼는데, ``bid`` 는 이 메서드(``_update_flood(self, c, t, frame, ps)``)의
스코프에 없고 **호출부(``_block_loop``)의 지역변수**였다. 이 줄이 메서드
전체를 감싸는 넓은 ``try/except`` 안, 그것도 위험 계산 **바로 앞**에 있어서
``NameError`` 가 조용히 삼켜지고 **그 아래 위험 계산 전부**
(``flood_engine.update`` → ``risk_engine.score`` → ``risk_predictor.update``
→ ``write_back`` → ``c["last_flood"]`` 갱신)가 **매 틱 통째로 건너뛰어졌다.**

기존 시험들은 ``push_boxes`` 호출 자체나 ``mask_bbox()`` 계산 결과만 봤고,
**``_update_flood()`` 를 실제로 끝까지 돌려 ``c["last_flood"]`` 가 갱신되는지
보는 시험이 없었다** — 그래서 회귀(1,399건)가 이 결함을 잡지 못한 채
통과했다. 이 시험은 그 빈틈을 메운다.
"""
from __future__ import annotations

import numpy as np

from tot_dashboard.common.notifier import AlertNotifier
from tot_dashboard.core import evidence as EV
from tot_dashboard.flood.flood_metrics_engine import FloodMetricsEngine
from tot_dashboard.flood.risk_engine import RiskEngine, RiskPredictor
from tot_dashboard.flood.water_segmentation import WaterResult
from tot_dashboard.models import TrafficMetrics, TrafficState
from tot_dashboard.service.runner import PipelineRunner


class _FakePS:
    """``Perception.step()`` 반환값 중 ``_update_flood`` 가 쓰는 부분만."""

    def __init__(self):
        self.vehicles = []
        self.persons = []
        self.metrics = TrafficMetrics(
            t_sec=0.0, n_vehicles=0, mean_speed=0.0, speed_drop=0.0,
            density=0.0, queue_len=0, stalled=0, state=TrafficState.free,
        )


def _fake_water_result() -> WaterResult:
    mask = np.zeros((60, 80), dtype=np.uint8)
    mask[10:20, 10:20] = 255  # 물로 판정된 영역이 있어야 mask_bbox()가 None이 아니다
    return WaterResult(mask=mask, num_instances=1, water_pixels=100, max_confidence=0.9)


def _make_ctx() -> dict:
    risk_engine = RiskEngine({}, {})
    return {
        "water_model": object(),  # None만 아니면 됨 — 실제 추론은 monkeypatch 로 대체
        "last_water_t": -999.0,
        "frame_no": 0,
        "corrupted_skips": 0,
        "flood_engine": FloodMetricsEngine(),
        "risk_engine": risk_engine,
        "risk_predictor": RiskPredictor(risk_engine, {}),
        "block": {"id": "TEST-RUNNER-FLOOD", "name": "시험지점"},
    }


def test_위험_계산이_끝까지_실행되고_last_flood가_갱신된다(monkeypatch, capsys):
    runner = object.__new__(PipelineRunner)  # __init__ 의 무거운 모델 적재 없이 메서드만 시험
    runner.water_cfg = {"process_every_seconds": 0.0, "water_conf": 0.10,
                        "iou": 0.50, "imgsz": 640}
    runner._water_backend_effective = ""  # segment_water(else 분기) 경로
    runner._water_device = "cpu"
    # ★ 2026-08-21: _update_flood()가 이제 _notify_flood_risk()를 호출하며
    #   self._notifier 를 넘긴다 — __init__ 을 건너뛰었으므로 직접 채운다.
    runner._notifier = AlertNotifier()

    monkeypatch.setattr("tot_dashboard.service.runner.segment_water",
                        lambda *a, **k: _fake_water_result())

    c = _make_ctx()
    frame = np.random.default_rng(0).integers(0, 255, (60, 80, 3), dtype=np.uint8)

    runner._update_flood(c, 1.0, frame, _FakePS())

    printed = capsys.readouterr().out
    # ★ 이게 이번에 잡은 회귀다 — 예전에는 여기서 매 틱
    #   "[runner:flood] name 'bid' is not defined" 가 찍히며 아래 assert가 전부 깨졌다.
    assert "[runner:flood]" not in printed, f"예외가 조용히 삼켜졌다: {printed!r}"
    assert c["last_flood"] is not None
    # ★ 2026-08-21: flood/traffic 분리로 c["last_flood"]가 4-튜플이 됐다
    #   (orchestrator가 조합하는 stopped_near_water가 4번째로 추가됨).
    flood_m, risk, pred, stopped_near_water = c["last_flood"]
    assert flood_m.risk_score == risk.risk_score
    assert pred is not None
    assert stopped_near_water == 0  # 시험용 차량이 없으므로


def test_push_boxes가_호출부가_아니라_실제_지점_id로_불린다(monkeypatch):
    """★ ``bid`` (호출부 지역변수)가 아니라 ``c["block"]["id"]`` 가 쓰이는지
    직접 확인한다 — 다른 카메라의 id 를 우연히 참조해도 이 시험은 잡지
    못하므로, 존재하지 않는 이름을 참조했다면 여기서 ``NameError`` 로 드러난다.
    """
    runner = object.__new__(PipelineRunner)
    runner.water_cfg = {"process_every_seconds": 0.0, "water_conf": 0.10,
                        "iou": 0.50, "imgsz": 640}
    runner._water_backend_effective = ""
    runner._water_device = "cpu"
    runner._notifier = AlertNotifier()

    monkeypatch.setattr("tot_dashboard.service.runner.segment_water",
                        lambda *a, **k: _fake_water_result())

    pushed = {}
    monkeypatch.setattr(EV, "push_boxes",
                        lambda cam, boxes, wh: pushed.update(cam=cam, boxes=boxes, wh=wh))

    c = _make_ctx()
    frame = np.random.default_rng(1).integers(0, 255, (60, 80, 3), dtype=np.uint8)
    runner._update_flood(c, 1.0, frame, _FakePS())

    assert pushed["cam"] == "TEST-RUNNER-FLOOD"
    assert pushed["boxes"] and pushed["boxes"][0]["label"] == "침수 영역"
