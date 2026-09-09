"""노면 실시간 관제 — 집중 감시 워처 (service/road_live.py, S-44).

상시 순회는 지점당 15분 주기라, 결빙·낙하물처럼 분 단위로 변하는 상황에는
대응할 수 없다. 집중 감시가 지켜야 할 것은 세 가지다.

* **한 번에 한 지점만** — 분석기가 하나뿐이라 여럿을 동시에 돌리면 상시
  순회까지 굶는다
* **자동 종료** — 켜 두고 잊으면 CPU를 계속 문다
* **결과를 같은 저장소에 남긴다** — 화면이 두 경로를 구분해 읽지 않아도 된다
"""
from __future__ import annotations

import time

import pytest

from tot_dashboard.road import results as R
from tot_dashboard.service import road_live


class FakeResult:
    def __init__(self, payload):
        self._p = payload

    def to_dict(self):
        return dict(self._p)


class FakeAnalyzer:
    """호출 횟수를 세는 가짜 분석기. 실제 모델·스트림을 쓰지 않는다."""

    def __init__(self, payload=None, raises=False):
        self.calls = []
        self.raises = raises
        self.payload = payload or {"grade": 1, "grade_label": "정상",
                                   "defects": [], "frames_analyzed": 3}

    def analyze(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise RuntimeError("모델 오류")
        return FakeResult(self.payload)


@pytest.fixture(autouse=True)
def clean():
    R.clear()
    yield
    road_live.manager.stop("테스트 정리")
    R.clear()


@pytest.fixture()
def fast(monkeypatch):
    """테스트에서만 주기 하한을 낮춘다(운영 하한은 30초)."""
    monkeypatch.setattr(road_live, "FOCUS_MIN_PERIOD_SEC", 0.01)
    monkeypatch.setattr(road_live, "FOCUS_DURATION_SEC", 0.0)


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- 주기·시한 --------------------------------------------------------------

def test_주기는_하한_아래로_내려가지_않는다():
    """1회 관측이 15초 걸린다. 그보다 짧게 두면 쉬는 구간 없이 계속 분석한다."""
    w = road_live.RoadFocusWatcher(FakeAnalyzer(), "CAM-A", "가지점", {},
                                   period_sec=1.0)
    assert w.period_sec == road_live.FOCUS_MIN_PERIOD_SEC


def test_주기는_상한을_넘지_않는다():
    w = road_live.RoadFocusWatcher(FakeAnalyzer(), "CAM-A", "가지점", {},
                                   period_sec=99999.0)
    assert w.period_sec == road_live.FOCUS_MAX_PERIOD_SEC


def test_시한이_지나면_스스로_멈춘다(fast):
    """켜 두고 잊는 것을 막는다."""
    a = FakeAnalyzer()
    w = road_live.RoadFocusWatcher(a, "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=0.05)
    w.start()
    assert _wait(lambda: not w.is_alive())
    assert "시한" in w.stop_reason
    assert a.calls, "시한 전에 최소 한 번은 관측해야 한다"


# --- 결과 기록 --------------------------------------------------------------

def test_관측_결과가_노면_저장소에_쌓인다(fast):
    a = FakeAnalyzer({"grade": 3, "grade_label": "주의",
                      "defects": [{"type": "pothole"}], "frames_analyzed": 4})
    w = road_live.RoadFocusWatcher(a, "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=0.4)
    w.start()
    assert _wait(lambda: R.get("CAM-A") is not None)
    w.stop()
    s = R.summary("CAM-A", "가지점")
    assert s["analyzed"] is True and s["defect_count"] == 1


def test_프레임을_못_받아도_그대로_기록한다(fast):
    """「지금 이 지점을 못 보고 있다」는 것도 관제에 필요한 정보다."""
    a = FakeAnalyzer({"grade": 1, "defects": [], "frames_analyzed": 0,
                      "note": "프레임을 받지 못했습니다."})
    w = road_live.RoadFocusWatcher(a, "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=0.4)
    w.start()
    assert _wait(lambda: R.get("CAM-A") is not None)
    w.stop()
    assert R.summary("CAM-A", "가지점")["failed"] is True


def test_분석이_터져도_감시가_죽지_않는다(fast):
    """한 번 실패했다고 감시가 통째로 멈추면 운영자는 이유를 알 수 없다."""
    a = FakeAnalyzer(raises=True)
    # 실패 뒤 휴식에는 1초 하한이 걸린다(오류 시 CPU를 물고 도는 것을 막는다).
    # 2회를 보려면 시한이 그보다 넉넉해야 한다.
    w = road_live.RoadFocusWatcher(a, "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=8.0)
    w.start()
    assert _wait(lambda: len(a.calls) >= 2, timeout=6.0)
    assert w.is_alive()
    assert "오류" in w.last_note
    w.stop()


def test_관측에_걸린_시간을_주기에서_뺀다(fast):
    """빼지 않으면 실제 주기가 「설정 주기 + 관측 시간」으로 늘어난다."""
    w = road_live.RoadFocusWatcher(FakeAnalyzer(), "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=0.05)
    started = time.time()
    w.start()
    w.join(timeout=5)
    # 관측이 즉시 끝나는 가짜 분석기이므로, 시한(0.05초)에 가깝게 끝나야 한다.
    assert time.time() - started < 2.0


# --- 한 번에 한 지점 ---------------------------------------------------------

def test_다른_지점을_켜면_이전_감시가_멈춘다(fast):
    """분석기가 하나뿐이라 둘을 동시에 돌리면 상시 순회까지 굶는다."""
    a = FakeAnalyzer()
    road_live.manager.start(a, "CAM-A", "가지점", {}, period_sec=0.01, ttl_sec=5)
    first = road_live.manager._watcher
    road_live.manager.start(a, "CAM-B", "나지점", {}, period_sec=0.01, ttl_sec=5)
    assert _wait(lambda: not first.is_alive())
    assert "전환" in first.stop_reason
    st = road_live.manager.status()
    assert st["active"] is True and st["camera_id"] == "CAM-B"
    road_live.manager.stop()


def test_중지하면_왜_멈췄는지_남는다(fast):
    road_live.manager.start(FakeAnalyzer(), "CAM-A", "가지점", {},
                            period_sec=0.01, ttl_sec=5)
    last = road_live.manager.stop()
    assert last is not None and "중지" in last["stop_reason"]
    st = road_live.manager.status()
    assert st["active"] is False
    assert st["last"]["camera_id"] == "CAM-A"


def test_감시_중이_아니면_중지는_아무_일도_하지_않는다():
    assert road_live.manager.stop() is None


def test_시한으로_끝난_감시도_상태에서_정리된다(fast):
    """스스로 죽은 스레드를 붙들고 있으면 계속 「감시 중」으로 보인다."""
    road_live.manager.start(FakeAnalyzer(), "CAM-A", "가지점", {},
                            period_sec=0.01, ttl_sec=0.05)
    assert _wait(lambda: road_live.manager.status()["active"] is False)
    assert road_live.manager.status()["last"]["camera_id"] == "CAM-A"


def test_상태에_남은_시한이_포함된다(fast):
    st = road_live.manager.start(FakeAnalyzer(), "CAM-A", "가지점", {},
                                 period_sec=0.01, ttl_sec=60)
    assert 0 < st["remaining_sec"] <= 60
    assert st["camera_name"] == "가지점"
    road_live.manager.stop()


# --- S-80 변경의 실시간 반영 (2026-08-12) ------------------------------------

def test_노면_대상에서_빠지면_집중_감시가_멈춘다(fast, monkeypatch):
    """S-80에서 「도로 노면 관리」를 꺼도 감시가 계속 돌면, 대상에서 뺀 지점을
    분석기가 붙잡고 있게 된다."""
    from tot_dashboard.core import config_rev

    config_rev.reset()
    monkeypatch.setattr(road_live, "_still_designated", lambda cid: False)
    w = road_live.RoadFocusWatcher(FakeAnalyzer(), "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=30)
    w.start()
    config_rev.bump()
    assert _wait(lambda: not w.is_alive(), timeout=6.0)
    assert "해제" in w.stop_reason
    config_rev.reset()


def test_설정이_그대로면_감시를_계속한다(fast, monkeypatch):
    from tot_dashboard.core import config_rev

    config_rev.reset()
    monkeypatch.setattr(road_live, "_still_designated", lambda cid: True)
    a = FakeAnalyzer()
    w = road_live.RoadFocusWatcher(a, "CAM-A", "가지점", {},
                                   period_sec=0.01, ttl_sec=30)
    w.start()
    try:
        config_rev.bump()
        assert _wait(lambda: len(a.calls) >= 2, timeout=6.0)
        assert w.is_alive()
    finally:
        w.stop()
        config_rev.reset()


def test_대상_확인에_실패하면_감시를_끊지_않는다(monkeypatch):
    """조회 실패로 운영자가 켜 둔 감시를 끊는 것이 더 나쁘다."""
    def _boom(*a, **k):
        raise RuntimeError("DB 없음")
    monkeypatch.setattr("tot_dashboard.core.db.get_session", _boom)
    assert road_live._still_designated("CAM-A") is True
