"""인파·노면 상시 탐지 워처 (service/continuous.py).

실제 스트림·모델 없이 검증하려고 cv2 와 분석기를 가짜로 갈아끼운다.
검증 대상은 **대상 선정과 주기 동작**이지 검출 정확도가 아니다.
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from tot_dashboard.core.db import get_session
from tot_dashboard.service import continuous as CT


@pytest.fixture(autouse=True)
def _clean_road_state():
    """노면 결과·설정 리비전을 테스트마다 비운다.

    워처는 「방금 본 지점」을 건너뛰므로, 앞선 테스트가 남긴 결과가 있으면
    다음 테스트에서 분석이 통째로 생략된다(실행 순서에 따라 깨졌다).
    """
    from tot_dashboard.core import config_rev
    from tot_dashboard.road import results as R

    R.clear()
    config_rev.reset()
    yield
    R.clear()
    config_rev.reset()


def _until(cond, timeout: float = 8.0) -> bool:
    """조건이 참이 될 때까지 기다린다. 워처는 별 스레드라 즉시 반영되지 않는다."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _isolate_watchers():
    """시험 사이에 워처 스레드를 **실제로 끝내고** 노면 결과를 비운다.

    ⚠️ 이게 없어서 **이 파일의 마지막 시험이 5번 중 4번 깨졌다.**

    두 가지가 겹친 문제였다.

    1. ``stop()`` 은 **신호만 보내고 스레드를 기다리지 않는다.** 시험이
       끝나도 앞선 워처가 한 바퀴를 마저 돌며 살아 있다.
    2. 저장소는 모듈 전역이고 이 파일의 여덟 시험이 **같은 카메라 id(``R1``)**
       를 쓴다. 살아남은 워처가 정리 뒤에 다시 기록을 남기면, 다음 시험의
       워처는 「직전 관측이 주기의 절반 안쪽」이라는 규칙
       (:meth:`RoadContinuousWatcher._recently_seen`)에 걸려 **관측 자체를
       건너뛴다.** 그래서 아무리 기다려도 조건이 참이 되지 않았다.

    처음에는 「느려서 시간이 모자란 것」으로 보고 대기 상한을 8→30초로
    올렸는데, **실패에 걸리는 시간만 늘 뿐 재현율은 그대로였다.** 상한을
    올려도 조건이 영영 참이 되지 않기 때문이다. 증상이 아니라 원인을
    고쳐야 했다.
    """
    import threading

    from tot_dashboard.road import results as road_results

    before = set(threading.enumerate())
    road_results.clear()
    yield
    # 이 시험이 띄운 스레드가 실제로 끝날 때까지 기다린다.
    for th in set(threading.enumerate()) - before:
        if th.is_alive() and th is not threading.current_thread():
            stop = getattr(th, "_stop_ev", None)
            if stop is not None:
                stop.set()
            th.join(timeout=5.0)
    road_results.clear()


class _Cam:
    def __init__(self, cid, name, source_type="hls", url="http://x/s.m3u8"):
        self.id, self.name = cid, name
        self.source_type, self.source_url = source_type, url
        self.source_path = ""


class _Snap:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class _Analyzer:
    def __init__(self):
        self.calls = []

    def step(self, t_sec, frame):
        self.calls.append(t_sec)
        return _Snap({"person_count": 3, "events": [], "source": "mock"})


@pytest.fixture
def fake_cv2(monkeypatch):
    class _Cap:
        def __init__(self, url):
            self.url = url

        def isOpened(self):
            return True

        def read(self):
            return True, object()

        def release(self):
            pass

    mod = types.ModuleType("cv2")
    mod.VideoCapture = _Cap
    monkeypatch.setitem(sys.modules, "cv2", mod)
    return mod


# --- CCTV 재배포 허브(MediaMTX) — 2026-08-28 신설 ----------------------------
#
# 인파는 `core/cameras.py::to_block_dict()`를 거치지 않고 `_stream_url()`이
# Camera ORM(여기서는 `_Cam` 가짜 객체)을 직접 읽으므로, 그 함수의 재배포
# 치환을 그대로 못 쓴다 — 여기서 별도로 확인한다.

@pytest.fixture
def _restream_reset(db_schema):
    # ★ autouse로 두지 않는다 — 이 파일의 다른 시험 대부분은 DB가 전혀
    # 필요 없다(가짜 cv2·가짜 카메라만으로 돈다). 재배포 설정을 만지는
    # 시험 3개에만 명시적으로 요청해, 나머지 시험들이 불필요하게 DB를
    # 요구하게 되는 것을 막는다.
    from tot_dashboard.core import settings as S

    def _off():
        db = get_session()
        try:
            S.set_restream_enabled(db, False)
            S.set_restream_excluded_ids(db, [])
            db.commit()
        finally:
            db.close()
        S.invalidate()

    _off()
    yield
    _off()


def test_stream_url_재배포_꺼져있으면_원본_URL_그대로다(_restream_reset):
    cam = _Cam("SEOUL-1042", "옥천교", source_type="hls", url="https://example.test/a.m3u8")
    assert CT._stream_url(cam) == "https://example.test/a.m3u8"


def test_stream_url_재배포_켜져있으면_RTSP로_치환된다(_restream_reset):
    from tot_dashboard.core import restream as RS
    from tot_dashboard.core import settings as S

    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    cam = _Cam("SEOUL-1042", "옥천교", source_type="hls", url="https://example.test/a.m3u8")
    assert CT._stream_url(cam) == RS.rtsp_url("SEOUL-1042")


def test_stream_url_재배포_켜져있어도_제외_목록이면_원본_URL_그대로다(_restream_reset):
    """★ 2026-08-28 실기 검증 중 발견 — Wowza 계열 원본 등 MediaMTX와
    근본적으로 안 맞는 카메라는 재배포가 전역으로 켜져 있어도 예외로
    원본 직결을 유지해야 한다."""
    from tot_dashboard.core import settings as S

    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_excluded_ids(db, ["SEOUL-1042"])
        db.commit()
    finally:
        db.close()

    cam = _Cam("SEOUL-1042", "옥천교", source_type="hls", url="https://example.test/a.m3u8")
    assert CT._stream_url(cam) == "https://example.test/a.m3u8"


def test_stream_url_설정_조회_실패해도_원본_URL로_안전하게_진행한다(monkeypatch, db_schema):
    from tot_dashboard.core import settings as S

    def _boom(*a, **k):
        raise RuntimeError("설정 조회 실패(시험)")

    monkeypatch.setattr(S, "restream_enabled", _boom)
    cam = _Cam("SEOUL-1042", "옥천교", source_type="hls", url="https://example.test/a.m3u8")
    assert CT._stream_url(cam) == "https://example.test/a.m3u8"


def test_영상_소스가_없는_카메라는_인파_상시_대상에서_빠진다(monkeypatch, fake_cv2):
    cams = [_Cam("A", "가", source_type="hls", url=""),      # URL 없음
            _Cam("B", "나")]
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: cams)
    monkeypatch.setattr(CT.CrowdContinuousWatcher, "_build_analyzer",
                        staticmethod(lambda block: _Analyzer()))
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "crowd": {}})

    w = CT.CrowdContinuousWatcher(interval_sec=0.5, start_delay_sec=0)
    try:
        w.start()
        assert [t[0] for t in w._targets] == ["B"]
    finally:
        w.stop()


def test_인파_워처는_카메라마다_별도_분석기를_쓴다(monkeypatch, fake_cv2):
    """하나를 공유하면 추적 ID와 배회 타이머가 지점 간에 뒤섞인다."""
    made = []

    def _build(block):
        a = _Analyzer()
        made.append((block.get("id"), a))
        return a

    monkeypatch.setattr(CT, "continuous_cameras",
                        lambda d: [_Cam("A", "가"), _Cam("B", "나")])
    monkeypatch.setattr(CT.CrowdContinuousWatcher, "_build_analyzer", staticmethod(_build))
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "crowd": {}})

    w = CT.CrowdContinuousWatcher(interval_sec=0.5, start_delay_sec=0)
    try:
        w.start()
        assert sorted(i for i, _ in made) == ["A", "B"]
        assert made[0][1] is not made[1][1]
    finally:
        w.stop()


def test_노면_워처는_DB에서_찾은_블록을_분석기에_넘긴다(monkeypatch):
    """blocks.json 에만 의존하면 S-80에서 새로 등록한 카메라를 못 찾는다."""
    seen = {}
    done = threading.Event()

    class _RoadAnalyzer:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            seen["mode"] = mode
            seen["target"] = target
            seen["block"] = block
            done.set()
            return _Snap({"defects": [], "grade": 1, "target": target})

    monkeypatch.setattr(CT, "continuous_cameras", lambda d: [_Cam("R1", "노면지점")])
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name,
                                     "source": {"type": "hls", "url": "http://x"}})

    w = CT.RoadContinuousWatcher(_RoadAnalyzer(), period_sec=30.0, duration_sec=0.1,
                                 start_delay_sec=0)
    try:
        w.start()
        assert done.wait(5), "노면 분석이 호출되지 않았다"
        assert seen["mode"] == "cctv"
        assert seen["target"] == "R1"
        assert seen["block"]["source"]["url"] == "http://x"
    finally:
        w.stop()


def test_노면은_프레임을_못_받으면_한_번_다시_시도한다(monkeypatch):
    """다음 순회까지 기다리면 주기(기본 15분)를 통째로 날린다."""
    calls = []

    class _Flaky:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            calls.append(target)
            if len(calls) == 1:      # 첫 시도는 프레임 0장
                return _Snap({"frames_analyzed": 0, "defects": [],
                              "note": "스트림에서 프레임을 받지 못했습니다."})
            return _Snap({"frames_analyzed": 4, "defects": [], "grade": 1})

    w = CT.RoadContinuousWatcher(_Flaky(), period_sec=30.0, duration_sec=0.1,
                                 start_delay_sec=0)
    monkeypatch.setattr(w._stop_ev, "wait", lambda *a, **k: False)
    d = w._analyze_once("R1", {"id": "R1"})

    assert calls == ["R1", "R1"], "재시도가 일어나지 않았다"
    assert d["frames_analyzed"] == 4


def test_노면_재시도도_실패하면_그_결과를_그대로_남긴다(monkeypatch):
    """무한 재시도로 순회가 멈추면 뒤쪽 카메라가 영영 분석되지 않는다."""
    calls = []

    class _Dead:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            calls.append(target)
            return _Snap({"frames_analyzed": 0, "defects": [], "note": "스트림 없음"})

    w = CT.RoadContinuousWatcher(_Dead(), period_sec=30.0, duration_sec=0.1,
                                 start_delay_sec=0)
    monkeypatch.setattr(w._stop_ev, "wait", lambda *a, **k: False)
    d = w._analyze_once("R1", {"id": "R1"})

    assert len(calls) == 2, "재시도는 한 번만이어야 한다"
    assert d["note"] == "스트림 없음"


def test_노면_상시_대상이_없어도_계속_대기한다(monkeypatch):
    """2026-08-12 동작 변경 — 예전에는 여기서 스레드가 **끝났다.**

    그러면 관리자가 나중에 S-80에서 「상시」로 지정해도 영영 반영되지 않는다.
    지금은 목록을 비운 채 대기하다가 설정이 바뀌면 다시 읽는다.
    """
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: [])
    w = CT.RoadContinuousWatcher(object(), period_sec=30.0, start_delay_sec=0)
    try:
        w.start()
        assert _until(lambda: w.status()["config_rev"] >= 0)
        assert w.is_alive(), "대상이 없다고 끝나면 나중 지정이 반영되지 않는다"
        assert w.status()["targets"] == []
    finally:
        w.stop()


def test_DB를_읽지_못해도_빈_목록으로_넘어간다(monkeypatch):
    """상시 탐지 대상 조회 실패가 서비스 기동을 막으면 안 된다."""
    def _boom(*a, **k):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr("tot_dashboard.core.db.get_session", _boom)
    assert CT.continuous_cameras("crowd") == []


def test_침수_목록은_사용_지정_전부를_싣는다(monkeypatch):
    """상시만 보여 주면 S-80에서 「선택」으로 지정한 지점이 화면에서 사라져,
    설정이 반영되지 않은 것처럼 보인다(사용자 지적, 2026-08-08)."""
    from tot_dashboard.service import main as M

    class _Row:
        def __init__(self, cont):
            self.continuous = cont
            self.enabled = True

    class _Cam:
        def __init__(self, cid, name, cont):
            self.id, self.name, self.dept = cid, name, "안전총괄과"
            self.lat, self.lng = 35.1, 129.0
            self._row = _Row(cont)

        def domain_row(self, domain):
            return self._row

        def roi_row(self, domain):
            return object()

    cams = [_Cam("A", "상시지점", True), _Cam("B", "선택지점", False)]
    monkeypatch.setattr("tot_dashboard.core.cameras.for_domain",
                        lambda db, dom, **k: cams)
    monkeypatch.setattr("tot_dashboard.core.db.get_session", lambda: _FakeSession())

    out = M.flood_cameras()
    assert [c["id"] for c in out] == ["A", "B"]
    assert [c["mode"] for c in out] == ["continuous", "selective"]


class _FakeSession:
    def close(self):
        pass


def test_노면_워처는_관측_프레임을_수집기로_넘긴다(monkeypatch):
    """수집기가 스트림을 따로 열면 같은 카메라에 세 번째로 붙는 셈이라
    CCTV 서버가 연결을 거절한다(실측). 그래서 분석기가 이미 뽑아 둔 프레임을
    받아 쓴다 — 이 통로가 끊기면 수집이 조용히 멈춘다."""
    got = {}
    done = threading.Event()
    captured = []

    class _Analyzer:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            got["sink"] = frame_sink
            if frame_sink is not None:
                frame_sink([(0, "프레임")])
            done.set()
            return _Snap({"defects": [], "grade": 1, "frames_analyzed": 1,
                          "target": target})

    monkeypatch.setattr(
        "tot_dashboard.road.dataset_collector.sink",
        lambda cid, name, source="continuous": (
            lambda frames: captured.append((cid, name, source, frames))))
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: [_Cam("R1", "노면지점")])
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_Analyzer(), period_sec=30.0, duration_sec=0.1,
                                 start_delay_sec=0)
    try:
        w.start()
        assert done.wait(5), "노면 분석이 호출되지 않았다"
        assert got["sink"] is not None, "수집기로 넘길 통로가 없다"
        assert captured and captured[0][:3] == ("R1", "노면지점", "continuous")
    finally:
        w.stop()


# ── S-80/S-81 변경의 실시간 반영 (2026-08-12) ────────────────────────────────
# 예전에는 기동 시점에 목록을 한 번만 읽어, 관리자가 「상시」로 지정해도 서비스를
# 다시 띄우기 전에는 아무도 그 지점을 보지 않았다. 화면에는 「상시」로 뜨는데
# 실제로는 관측되지 않는 상태 — 그러면 운영자가 설정을 믿을 수 없게 된다.

def _road_analyzer(seen, ev=None):
    class _A:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            seen.append(target)
            if ev is not None:
                ev.set()
            return _Snap({"defects": [], "grade": 1, "frames_analyzed": 1,
                          "target": target})
    return _A()


def test_대상_목록을_매_바퀴_다시_읽는다(monkeypatch):
    from tot_dashboard.core import config_rev
    from tot_dashboard.road import results as R

    R.clear()
    config_rev.reset()
    seen = []
    cams = [_Cam("R1", "가지점")]
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: list(cams))
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_road_analyzer(seen), period_sec=30.0,
                                 duration_sec=0.1, start_delay_sec=0)
    try:
        w.start()
        _until(lambda: "R1" in seen)
        # 관리자가 S-80에서 새 지점을 「상시」로 지정했다.
        cams.append(_Cam("R2", "나지점"))
        config_rev.bump()
        assert _until(lambda: "R2" in seen), "새로 지정한 지점이 잡히지 않았다"
    finally:
        w.stop()
        R.clear()


def test_대상에서_빼면_더_이상_보지_않는다(monkeypatch):
    from tot_dashboard.core import config_rev
    from tot_dashboard.road import results as R

    R.clear()
    config_rev.reset()
    seen = []
    cams = [_Cam("R1", "가지점"), _Cam("R2", "나지점")]
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: list(cams))
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_road_analyzer(seen), period_sec=30.0,
                                 duration_sec=0.1, start_delay_sec=0)
    try:
        w.start()
        _until(lambda: "R2" in seen)
        cams.pop()                      # S-80에서 R2 의 노면 사용을 껐다
        config_rev.bump()
        assert _until(lambda: [t["id"] for t in w.status()["targets"]] == ["R1"])
    finally:
        w.stop()
        R.clear()


def test_대상이_하나도_없어도_워처가_죽지_않는다(monkeypatch):
    """예전에는 여기서 스레드가 끝나 버려, 나중에 지정해도 영영 반영되지
    않았다 — 「켰는데 아무 일도 없다」의 원인이다."""
    from tot_dashboard.core import config_rev
    from tot_dashboard.road import results as R

    R.clear()
    config_rev.reset()
    seen = []
    cams: list = []
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: list(cams))
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_road_analyzer(seen), period_sec=30.0,
                                 duration_sec=0.1, start_delay_sec=0)
    try:
        w.start()
        _until(lambda: w.status()["config_rev"] >= 0)
        assert w.is_alive(), "대상이 없다고 워처가 끝나면 안 된다"
        cams.append(_Cam("R1", "가지점"))
        config_rev.bump()
        assert _until(lambda: "R1" in seen)
    finally:
        w.stop()
        R.clear()


def test_방금_본_지점은_다시_돌리지_않는다(monkeypatch):
    """설정을 연달아 고치면 그때마다 목록을 다시 읽는데, 매번 전부 재분석하면
    같은 카메라를 계속 붙잡고 있게 된다."""
    from tot_dashboard.core import config_rev
    from tot_dashboard.road import results as R

    R.clear()
    config_rev.reset()
    seen = []
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: [_Cam("R1", "가지점")])
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_road_analyzer(seen), period_sec=600.0,
                                 duration_sec=0.1, start_delay_sec=0)
    try:
        w.start()
        _until(lambda: len(seen) >= 1)
        for _ in range(3):
            config_rev.bump()
            time.sleep(0.3)
        assert len(seen) == 1, f"방금 본 지점을 다시 분석했다: {seen}"
    finally:
        w.stop()
        R.clear()


def test_순회_진행_상황을_상태에_싣는다(monkeypatch):
    """한 바퀴에 「대상 수 × 15초」가 걸린다. 지금 어디를 보고 있는지가
    안 보이면 운영자는 15분 동안 그대로인 화면을 보고 멈춘 줄 안다."""
    from tot_dashboard.core import config_rev

    config_rev.reset()
    entered = threading.Event()
    release = threading.Event()

    class _Slow:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            entered.set()
            release.wait(5)
            return _Snap({"defects": [], "grade": 1, "frames_analyzed": 1,
                          "target": target})

    monkeypatch.setattr(CT, "continuous_cameras",
                        lambda d: [_Cam("R1", "가지점"), _Cam("R2", "나지점")])
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_Slow(), period_sec=30.0, duration_sec=0.1,
                                 start_delay_sec=0)
    try:
        w.start()
        assert entered.wait(5), "분석이 시작되지 않았다"
        st = w.status()
        assert st["target_count"] == 2
        cur = st["current"]
        assert cur is not None, "관측 중인데 현재 지점이 비어 있다"
        assert (cur["id"], cur["index"], cur["total"]) == ("R1", 1, 2)
        assert cur["elapsed_sec"] >= 0
        assert st["round"] >= 1
    finally:
        release.set()
        w.stop()


def test_쉬는_동안에는_현재_지점이_비고_남은_시간이_찬다(monkeypatch):
    from tot_dashboard.core import config_rev

    config_rev.reset()
    seen = []
    monkeypatch.setattr(CT, "continuous_cameras", lambda d: [_Cam("R1", "가지점")])
    monkeypatch.setattr("tot_dashboard.core.cameras.to_block_dict",
                        lambda cam: {"id": cam.id, "name": cam.name})

    w = CT.RoadContinuousWatcher(_road_analyzer(seen), period_sec=600.0,
                                 duration_sec=0.1, start_delay_sec=0)
    try:
        w.start()
        assert _until(lambda: w.status()["current"] is None and seen)
        st = w.status()
        assert st["resume_in_sec"] is not None and st["resume_in_sec"] > 0
    finally:
        w.stop()


# --- ⚠️ 회귀: 노면 지점도 증거 링 버퍼에 프레임을 넘긴다 (S-88, 2026-08-20) --
#
# 예전에는 이 호출이 아예 없었다 — 인파는 `_loop` 에서 매 틱
# `_evidence.push` 를 부르는데, 노면 상시 순회에는 그 호출이 처음부터
# 없었다. 그 결과 **노면 이벤트는 증거(정지영상·클립)가 한 번도 남을 수
# 없는 구조**였다.


# ── 인파 상시 검출 소스 전역 기본값 (2026-08-27, 카메라별 모니터링 신설) ────
#
# CrowdContinuousWatcher._build_analyzer()가 실제 CrowdLiveAnalyzer를
# 만들면 torch/torchvision을 불러오려 하므로(detector 모드), 여기서는
# CrowdLiveAnalyzer 자체를 가짜로 갈아끼워 **어떤 cfg로 불렸는지만** 본다.

class _FakeCrowdLiveAnalyzer:
    """실제 모델을 불러오지 않고 cfg만 기록한다."""
    last_cfg: dict | None = None

    def __init__(self, cfg=None, block_id=None, node_id=None, fps=1.0):
        _FakeCrowdLiveAnalyzer.last_cfg = dict(cfg or {})
        self.block_id = block_id


@pytest.fixture
def _reset_crowd_source_setting(db):
    """설정 캐시는 프로세스 전역이라 앞 시험이 바꿔 놓은 값이 새어 들어온다."""
    from sqlalchemy import delete as sa_delete

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    def _clear():
        db.execute(sa_delete(AppSetting).where(
            AppSetting.key == ug_settings.KEY_CROWD_CONTINUOUS_SOURCE))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


@pytest.fixture
def db():
    from tot_dashboard.core.db import get_session
    s = get_session()
    yield s
    s.close()


def _patch_fake_analyzer(monkeypatch):
    monkeypatch.setattr("tot_dashboard.crowd.live_analyzer.CrowdLiveAnalyzer",
                        _FakeCrowdLiveAnalyzer)
    monkeypatch.setattr("tot_dashboard.crowd.live_analyzer.mock_restricted_roi",
                        lambda: [])


def test_카메라가_명시하지_않으면_전역_기본값_detector를_쓴다(monkeypatch, db_schema, db, _reset_crowd_source_setting):
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["type"] == "detector"


def test_전역_기본값을_mock으로_바꾸면_명시_안한_카메라가_따른다(monkeypatch, db_schema, db, _reset_crowd_source_setting):
    from tot_dashboard.core import settings as ug_settings

    ug_settings.set_crowd_continuous_source(db, "mock")
    db.commit()
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["type"] == "mock"


def test_카메라가_명시한_소스는_전역_기본값보다_우선한다(monkeypatch, db_schema, db, _reset_crowd_source_setting):
    from tot_dashboard.core import settings as ug_settings

    ug_settings.set_crowd_continuous_source(db, "mock")   # 전역은 mock인데
    db.commit()
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer(
        {"id": "A", "crowd": {"source": {"type": "detector"}}})
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["type"] == "detector"


def test_설정_조회가_실패해도_detector로_fail_open한다(monkeypatch, db_schema, db, _reset_crowd_source_setting):
    """DB가 잠깐 안 되는 것 때문에 워처 기동 전체가 죽으면 안 된다."""
    def _boom(*a, **k):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr("tot_dashboard.core.db.get_session", _boom)
    _patch_fake_analyzer(monkeypatch)
    a = CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert a is not None
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["type"] == "detector"


# --- 인파 타일 격자 (2026-08-29 신설, 로드맵 1단계) ---------------------------

@pytest.fixture
def _reset_crowd_tile_grid_setting(db):
    from sqlalchemy import delete as sa_delete

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    def _clear():
        db.execute(sa_delete(AppSetting).where(
            AppSetting.key == ug_settings.KEY_CROWD_TILE_GRID))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


def test_카메라가_명시하지_않으면_전역_기본_타일격자_2x2를_쓴다(
        monkeypatch, db_schema, db, _reset_crowd_source_setting, _reset_crowd_tile_grid_setting):
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["tile_grid"] == [2, 2]


def test_전역_타일격자를_3x3으로_바꾸면_명시_안한_카메라가_따른다(
        monkeypatch, db_schema, db, _reset_crowd_source_setting, _reset_crowd_tile_grid_setting):
    from tot_dashboard.core import settings as ug_settings

    ug_settings.set_crowd_tile_grid(db, "3x3")
    db.commit()
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["tile_grid"] == [3, 3]


def test_카메라가_명시한_타일격자는_전역_기본값보다_우선한다(
        monkeypatch, db_schema, db, _reset_crowd_source_setting, _reset_crowd_tile_grid_setting):
    from tot_dashboard.core import settings as ug_settings

    ug_settings.set_crowd_tile_grid(db, "4x4")   # 전역은 4x4인데
    db.commit()
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer(
        {"id": "A", "crowd": {"source": {"type": "detector", "tile_grid": [2, 2]}}})
    assert _FakeCrowdLiveAnalyzer.last_cfg["source"]["tile_grid"] == [2, 2]


def test_mock_소스는_타일격자_설정을_조회하지_않는다(
        monkeypatch, db_schema, db, _reset_crowd_source_setting, _reset_crowd_tile_grid_setting):
    """detector가 아니면 안 쓰이는 값이라 DB 조회 자체를 건너뛴다 — 불필요한
    조회를 없애 mock 모드에서 매번 카메라 기동 때마다 DB를 두 번 두드리지
    않는다."""
    from tot_dashboard.core import settings as ug_settings

    ug_settings.set_crowd_continuous_source(db, "mock")
    db.commit()
    _patch_fake_analyzer(monkeypatch)
    CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert "tile_grid" not in _FakeCrowdLiveAnalyzer.last_cfg["source"]


def test_타일격자_설정_조회가_실패해도_2x2로_fail_open한다(
        monkeypatch, db_schema, db, _reset_crowd_source_setting, _reset_crowd_tile_grid_setting):
    """DB가 잠깐 안 되는 것 때문에 워처 기동 전체가 죽으면 안 된다 — 검출
    소스 fail-open과 같은 원칙."""
    from tot_dashboard.core import settings as ug_settings

    ug_settings.set_crowd_tile_grid(db, "3x3")
    db.commit()

    from tot_dashboard.core.db import get_session as real_get_session
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get_session(*a, **k)  # crowd_continuous_source 조회는 통과
        raise RuntimeError("DB 없음")           # tile_grid 조회만 실패

    monkeypatch.setattr("tot_dashboard.core.db.get_session", _flaky)
    _patch_fake_analyzer(monkeypatch)
    a = CT.CrowdContinuousWatcher._build_analyzer({"id": "A", "crowd": {}})
    assert a is not None
    assert "tile_grid" not in _FakeCrowdLiveAnalyzer.last_cfg["source"]


def test_노면도_증거_링_버퍼에_프레임을_넘긴다(monkeypatch):
    import numpy as np

    from tot_dashboard.core import evidence as EV

    frame = np.zeros((20, 30, 3), dtype=np.uint8)

    # RoadAnalysisResult 흉내 — to_dict() 는 evidence_* 를 안 담지만(직렬화
    # 안 함) continuous.py 는 res 자체의 속성으로 읽는다.
    class _Res:
        def to_dict(self):
            return {"frames_analyzed": 1, "defects": [], "grade": 1}
        evidence_frame = frame
        evidence_boxes = [{"x1": 1, "y1": 1, "x2": 2, "y2": 2, "label": "pothole"}]

    class _RoadAnalyzerWithEvidence:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            return _Res()

    EV.reset()
    w = CT.RoadContinuousWatcher(_RoadAnalyzerWithEvidence(), period_sec=30.0,
                                 duration_sec=0.1, start_delay_sec=0)
    monkeypatch.setattr(w._stop_ev, "wait", lambda *a, **k: False)
    w._analyze_once("R-EVID", {"id": "R-EVID"})

    assert EV.buffered("R-EVID") == 1, "노면 프레임이 증거 링 버퍼에 안 담겼습니다"
    boxes, fw, fh = EV.latest_boxes("R-EVID")
    assert boxes == [{"x1": 1, "y1": 1, "x2": 2, "y2": 2, "label": "pothole"}]
    assert (fw, fh) == (30, 20)     # frame.shape[1], frame.shape[0]
    EV.reset()


def test_손상이_없으면_상자도_안_남긴다(monkeypatch):
    """빈 상자를 넘겨도 터지지 않고, 링 버퍼에는 여전히 프레임이 남는다."""
    import numpy as np

    from tot_dashboard.core import evidence as EV

    frame = np.zeros((10, 10, 3), dtype=np.uint8)

    class _Res:
        def to_dict(self):
            return {"frames_analyzed": 1, "defects": []}
        evidence_frame = frame
        evidence_boxes = []

    class _RoadAnalyzerNoDefects:
        def analyze(self, *, mode, target, duration_sec, block, frame_sink=None):
            return _Res()

    EV.reset()
    w = CT.RoadContinuousWatcher(_RoadAnalyzerNoDefects(), period_sec=30.0,
                                 duration_sec=0.1, start_delay_sec=0)
    monkeypatch.setattr(w._stop_ev, "wait", lambda *a, **k: False)
    w._analyze_once("R-EMPTY", {"id": "R-EMPTY"})

    assert EV.buffered("R-EMPTY") == 1
    boxes, _, _ = EV.latest_boxes("R-EMPTY")
    assert boxes == []
    EV.reset()
