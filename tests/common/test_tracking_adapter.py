"""추적기 어댑터 (common/tracking.py).

``supervision.ByteTrack`` 이 **0.31.0 에서 제거**되고 후속은 별도 패키지
``trackers`` 의 ``ByteTrackTracker`` 다. 메서드 이름도 바뀌었다
(``update_with_detections()`` → ``update()``).

지켜야 할 것.

* **두 구현이 같은 값을 낸다** — 교체로 판정이 달라지면 안 된다
* **미확정 트랙을 걸러 낸다** — ``trackers`` 는 미확정에 ``tracker_id = -1``
  을 붙여 돌려준다. 그대로 두면 **전부 「id −1 인 한 트랙」으로 뭉쳐**
  속도가 터무니없이 커진다(실측 41,231 px/s)
* **추적기가 없어도 죽지 않는다** — 인원수·밀집도는 계속 나와야 한다
* **폐기 경고가 새어 나오지 않는다** — 매 프레임 찍히면 로그가 묻힌다
"""
from __future__ import annotations

import warnings

import numpy as np
import pytest
import supervision as sv

from tot_dashboard.common import tracking
from tot_dashboard.crowd.behavior_tracker import CrowdBehaviorTracker


def _det(boxes, conf=0.9):
    return sv.Detections(
        xyxy=np.asarray(boxes, np.float32),
        confidence=np.full(len(boxes), conf, np.float32),
        class_id=np.zeros(len(boxes), int))


# --- 선택 --------------------------------------------------------------------
def test_후속_패키지가_있으면_그쪽을_쓴다():
    t = tracking.Tracker(fps=10)
    assert t.backend in (tracking.TRACKERS, tracking.SUPERVISION)
    if t.backend == tracking.SUPERVISION:
        pytest.skip("trackers 패키지가 설치돼 있지 않다")
    assert t.available


def test_어느_구현인지_밖에서_볼_수_있다():
    """결과가 달라지면 여기부터 찾게 된다."""
    t = tracking.Tracker(fps=10)
    assert tracking.describe(t.backend)


def test_폐기_경고가_새어_나오지_않는다():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        tracking.Tracker(fps=10)
    msgs = " ".join(str(x.message) for x in w)
    assert "deprecated" not in msgs.lower(), (
        "매 프레임 찍히면 정작 봐야 할 로그가 묻힌다")


# --- 미확정 트랙 가리기 — 이번 이전의 핵심 -----------------------------------
def test_미확정_트랙을_가려_낸다():
    """``trackers`` 는 미확정에 -1 을 준다. 속도 계산에서 빼야 한다."""
    assert tracking.is_confirmed(3)
    assert tracking.is_confirmed(0)
    assert not tracking.is_confirmed(-1)
    assert not tracking.is_confirmed(None)


def test_어댑터는_검출을_버리지_않는다():
    """사람을 세는 일과 속도를 재는 일은 다르다.

    미확정이라고 **화면의 인원수가 0이 되면 안 된다.** 실제로 걸러 봤다가
    첫 프레임 인원이 0으로 나오는 퇴행이 생겨 되돌렸다.
    """
    t = tracking.Tracker(fps=10)
    if not t.available:
        pytest.skip("추적기가 없다")
    out = t.update(_det([[0, 0, 10, 10], [20, 20, 30, 30]]))
    assert len(out) == 2, "추적 확정 여부와 무관하게 검출 수는 유지돼야 한다"


# --- 두 구현의 값이 같은가 — 이전의 전제 -------------------------------------
def _walk(tracker_obj, force_sv: bool):
    t = CrowdBehaviorTracker(fps=10)
    if force_sv:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            impl = sv.ByteTrack(frame_rate=10, track_activation_threshold=0.25)
        t.tracker._call = impl.update_with_detections
        t.tracker.backend = tracking.SUPERVISION
    last = None
    for i in range(6):
        boxes = np.array([[10 + i * 5, 10, 30 + i * 5, 60],
                          [50 - i * 5, 20, 70 - i * 5, 70]], np.float32)
        last, _ = t.update(boxes, np.array([0.9, 0.85], np.float32), i * 0.1)
    return last


def test_교체_전후_지표가_같다():
    """교체로 판정이 달라지면 그동안의 관제 이력과 비교할 수 없게 된다."""
    if tracking.Tracker(fps=10).backend != tracking.TRACKERS:
        pytest.skip("trackers 패키지가 설치돼 있지 않다")
    old = _walk(None, force_sv=True)
    new = _walk(None, force_sv=False)
    for k in ("n_tracks", "mean_speed", "surge", "dispersion", "divergence"):
        assert old[k] == pytest.approx(new[k], rel=1e-3, abs=1e-3), (
            f"{k} 가 달라졌다: 이전 {old[k]} / 이후 {new[k]}")


def test_속도가_터무니없지_않다():
    """미확정 트랙이 섞이면 좌표가 급점프해 속도가 폭주한다."""
    m = _walk(None, force_sv=False)
    assert m["mean_speed"] < 1000, (
        f"속도 {m['mean_speed']:.0f} px/s — 미확정 트랙이 섞였을 때의 증상이다")


# --- 추적기가 없을 때 --------------------------------------------------------
def test_추적기가_없어도_인원수는_나온다(monkeypatch):
    t = tracking.Tracker(fps=10)
    monkeypatch.setattr(t, "_call", None)
    d = _det([[0, 0, 10, 10], [20, 20, 30, 30]])
    out = t.update(d)
    assert len(out) == 2, "추적이 안 돼도 사람 수는 세야 한다"


def test_추적_실패는_프레임을_건너뛸_뿐이다(monkeypatch):
    t = tracking.Tracker(fps=10)

    def boom(_):
        raise RuntimeError("추적기 내부 오류")

    monkeypatch.setattr(t, "_call", boom)
    d = _det([[0, 0, 10, 10]])
    assert len(t.update(d)) == 1, "한 프레임 실패로 관제가 멈추면 안 된다"
