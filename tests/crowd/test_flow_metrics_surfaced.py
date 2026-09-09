"""인파 흐름 지표와 판정 근거의 노출 (crowd/live_analyzer.py · semantic_risk_agent.py).

무엇을 지키나

* **위험도 점수가 0으로 죽지 않는다** — 판정기는 ``score`` 를 돌려주는데 받는
  쪽이 ``risk_score`` 를 찾고 있어 **점수가 항상 0.0** 이었다. 등급은 제 키를
  써서 정상이라 눈에 띄지 않았다
* **판정 근거가 화면까지 간다** — 「군중급증 0.72」만 보이면 관제요원이 오탐인지
  실제인지 가릴 수 없다
* **흐름 지표가 스냅샷에 담긴다** — 넷 다 이미 위험도 산출에 쓰이는데
  ``mean_speed`` 와 ``dispersion`` 만 밖으로 나갔다
"""
from __future__ import annotations

import numpy as np
import pytest

from tot_dashboard.crowd.behavior_tracker import CrowdBehaviorTracker
from tot_dashboard.crowd.live_analyzer import CrowdSnapshot
from tot_dashboard.crowd.semantic_risk_agent import SemanticRiskAgent


# --- 판정기 계약 -------------------------------------------------------------
def test_판정기는_score_키로_점수를_돌려준다():
    """받는 쪽이 다른 키를 찾으면 점수가 조용히 0이 된다."""
    r = SemanticRiskAgent().assess(
        {"density_pct": 70.0},
        {"surge": 2.0, "dispersion": 0.9, "divergence": 10.0}, "")
    assert "score" in r, "판정기 반환 키가 바뀌면 소비처를 함께 고쳐야 한다"
    assert r["score"] > 0


def test_판정기는_근거를_함께_돌려준다():
    r = SemanticRiskAgent().assess(
        {"density_pct": 70.0},
        {"surge": 2.0, "dispersion": 0.9, "divergence": 10.0}, "")
    assert r.get("drivers"), "근거가 없으면 화면이 「왜」에 답할 수 없다"


def test_흐름_지표가_실제로_점수를_움직인다():
    """넷 다 가중치를 갖는다. 하나라도 무시되면 판정이 둔해진다."""
    a = SemanticRiskAgent()
    base = {"surge": 1.0, "dispersion": 0.0, "divergence": 0.0}
    quiet = a.assess({"density_pct": 10.0}, dict(base), "")["score"]
    for key, hot in (("surge", 2.5), ("dispersion", 1.0), ("divergence", 12.0)):
        b = dict(base)
        b[key] = hot
        got = a.assess({"density_pct": 10.0}, b, "")["score"]
        assert got > quiet, f"{key} 가 점수에 반영되지 않는다"


# --- 스냅샷 ------------------------------------------------------------------
def _snap(**kw) -> CrowdSnapshot:
    base = dict(t_sec=1.0, person_count=5, density_index=0.4, mean_speed=12.0,
                trajectory_variance=0.3, action_label="정상",
                risk_code="NORMAL", risk_name="정상", risk_score=0.0, severity=0)
    base.update(kw)
    return CrowdSnapshot(**base)


def test_스냅샷이_흐름_지표를_담는다():
    d = _snap(surge=1.8, divergence=7.5).to_dict()
    assert d["surge"] == 1.8
    assert d["divergence"] == 7.5


def test_스냅샷이_판정_근거를_담는다():
    d = _snap(drivers=["발산도UP", "속도급증"]).to_dict()
    assert d["drivers"] == ["발산도UP", "속도급증"]


def test_근거가_없어도_키는_있다():
    """화면이 없는 키를 참조하면 그 카드가 통째로 안 그려진다."""
    d = _snap().to_dict()
    assert d["drivers"] == []
    assert "surge" in d and "divergence" in d


def test_기본값은_평상시를_뜻한다():
    """surge 기본값이 0이면 「속도가 0배」라는 뜻이 되어 판정이 뒤집힌다."""
    s = _snap()
    assert s.surge == 1.0, "1.0 이 평상시다"
    assert s.divergence == 0.0


# --- 추적기 ------------------------------------------------------------------
def test_추적기가_네_지표를_모두_돌려준다():
    t = CrowdBehaviorTracker(fps=10)
    boxes = np.array([[10, 10, 30, 60], [50, 20, 70, 70]], np.float32)
    scores = np.array([0.9, 0.8], np.float32)
    m, _ = t.update(boxes, scores, 0.0)
    boxes2 = boxes + np.array([[5, 0, 5, 0], [-5, 0, -5, 0]], np.float32)
    m, _ = t.update(boxes2, scores, 0.1)
    for k in ("n_tracks", "mean_speed", "surge", "dispersion", "divergence"):
        assert k in m, f"{k} 가 빠지면 판정기가 KeyError 로 죽는다"


def test_사람이_없으면_평상시_값을_준다():
    t = CrowdBehaviorTracker(fps=10)
    m, _ = t.update(np.zeros((0, 4), np.float32), np.zeros(0, np.float32), 0.0)
    assert m["n_tracks"] == 0
    assert m["surge"] == 1.0, "빈 화면이 급증으로 읽히면 안 된다"


# --- 화면 --------------------------------------------------------------------
def test_화면이_근거와_흐름_지표를_그린다():
    """서버가 보내도 화면이 안 쓰면 아무 일도 안 일어난다."""
    import pathlib

    js = (pathlib.Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
          / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function crowdDrivers(" in js
    assert "crowdDrivers(d)" in js, "만들어 놓고 부르지 않으면 안 보인다"
    assert "d.surge" in js and "d.divergence" in js
    # 화면에 넣는 값은 이스케이프한다.
    assert "function esc(" in js and "esc(x)" in js


def test_근거_칩에_스타일이_있다():
    import pathlib

    css = (pathlib.Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
           / "service" / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".crowd-drivers" in css
