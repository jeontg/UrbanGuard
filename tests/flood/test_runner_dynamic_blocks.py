"""``PipelineRunner`` 가 상시 탐지 목록을 재기동 없이 재구성하는가
(2026-08-22 전수점검).

## 왜 이 시험이 있나

S-80 에서 카메라를 새로 등록하고 「침수 = 사용 + 상시」로 지정해도
**서비스를 다시 띄우기 전에는 아무 일도 일어나지 않았다.** 화면에는
「상시」로 뜨는데 실제로는 아무도 그 지점을 보지 않는 상태 — 노면 워처는
이미 ``config_rev`` 를 구독해 이 문제가 없었는데 침수만 빠져 있었다.

지켜야 할 것.

* 상시로 새로 켠 지점은 **워커가 새로 뜬다**
* 상시에서 뺀 지점은 **워커가 멈추고 상황판에서도 사라진다**
  (마지막 스냅샷이 남아 있으면 멈춘 값을 현재 상태로 읽게 된다)
* ``dynamic=False`` 면 **아무리 신호가 와도 목록이 바뀌지 않는다**
  — 시험 환경(``TOT_BLOCKS_PATH`` 고정)이 이 경로를 쓰므로, 이것이
  깨지면 기존 시험 전체가 예측 불가능해진다
"""
from __future__ import annotations

import threading

import pytest

from tot_dashboard.service.runner import PipelineRunner
from tot_dashboard.service.store import RiskStore


def _block(bid: str) -> dict:
    return {"id": bid, "name": f"{bid}지점",
            "coordinates": {"lat": 35.1, "lng": 129.0},
            "source": {"type": "synthetic"}}


def _make_runner(*, dynamic: bool) -> PipelineRunner:
    """무거운 ``__init__`` (모델 적재·스트림 연결) 없이 재구성 로직만 시험."""
    r = object.__new__(PipelineRunner)
    r.store = RiskStore()
    r.fps = 5.0
    r.dt = 0.2
    r.use_vlm = False
    r.dynamic = dynamic
    r._stop_ev = threading.Event()
    r._ctx = {}
    r._ctx_lock = threading.RLock()
    r._workers = []
    return r


@pytest.fixture()
def fake_ctx(monkeypatch):
    """``_build_block_ctx`` 를 가볍게 대체하고, 워커 스레드는 띄우지 않는다.

    실제 스레드를 띄우면 합성 소스가 계속 돌아 시험이 느려지고 서로
    간섭한다 — 여기서 보려는 것은 **목록 재구성**이지 프레임 처리가 아니다.
    """
    started: list[str] = []

    def _fake_build(self, b):
        return {"block": b, "stop_ev": threading.Event(), "thread": None}

    def _fake_start(self, bid, c):
        started.append(bid)

    monkeypatch.setattr(PipelineRunner, "_build_block_ctx", _fake_build)
    monkeypatch.setattr(PipelineRunner, "_start_worker", _fake_start)
    return started


# --- RiskStore.remove -------------------------------------------------------

def test_store_remove가_현재값과_이력을_함께_지운다():
    st = RiskStore()
    st.update("A", {"t_sec": 1.0, "severity": 0, "speed_drop": 0.0,
                    "rain_mm_h": 0.0, "name": "가"})
    assert st.get("A") is not None
    assert "A" in st.all_history()

    assert st.remove("A") is True
    assert st.get("A") is None
    assert "A" not in st.all_history()
    # 두 번째는 지울 것이 없다 — 호출부가 "정말 있었나"를 구분할 수 있어야 한다.
    assert st.remove("A") is False


# --- dynamic=False (기존 동작 보존) ------------------------------------------

def test_dynamic이_꺼져_있으면_재조정이_아무것도_안_한다(fake_ctx, monkeypatch):
    """★ 시험 환경이 이 경로를 쓴다 — 깨지면 기존 시험 전체가 흔들린다."""
    r = _make_runner(dynamic=False)
    r._ctx["OLD"] = {"block": _block("OLD"), "stop_ev": threading.Event(),
                     "thread": None}

    called = []
    monkeypatch.setattr(PipelineRunner, "_desired_blocks",
                        lambda self: called.append(1) or {})

    r._reconcile()

    assert called == [], "dynamic=False 인데 DB를 조회했다"
    assert r.block_ids() == {"OLD"}, "dynamic=False 인데 목록이 바뀌었다"


# --- dynamic=True (동적 재구성) ----------------------------------------------

def test_상시로_새로_켠_지점은_워커가_새로_뜬다(fake_ctx, monkeypatch):
    r = _make_runner(dynamic=True)
    monkeypatch.setattr(PipelineRunner, "_desired_blocks",
                        lambda self: {"NEW": _block("NEW")})

    r._reconcile()

    assert r.block_ids() == {"NEW"}
    assert fake_ctx == ["NEW"], "새 지점의 워커가 뜨지 않았다"


def test_상시에서_뺀_지점은_멈추고_상황판에서도_사라진다(fake_ctx, monkeypatch):
    r = _make_runner(dynamic=True)
    ctx = {"block": _block("GONE"), "stop_ev": threading.Event(), "thread": None}
    r._ctx["GONE"] = ctx
    r.store.update("GONE", {"t_sec": 1.0, "severity": 0, "speed_drop": 0.0,
                            "rain_mm_h": 0.0, "name": "사라질지점"})

    monkeypatch.setattr(PipelineRunner, "_desired_blocks", lambda self: {})
    r._reconcile()

    assert r.block_ids() == set()
    assert ctx["stop_ev"].is_set(), "그 지점의 정지 신호를 세우지 않았다"
    # ★ 안 지우면 멈춘 값이 상황판에 현재 상태처럼 남는다.
    assert r.store.get("GONE") is None


def test_추가와_제외가_한_번에_일어나도_맞춰진다(fake_ctx, monkeypatch):
    r = _make_runner(dynamic=True)
    r._ctx["KEEP"] = {"block": _block("KEEP"), "stop_ev": threading.Event(),
                      "thread": None}
    r._ctx["DROP"] = {"block": _block("DROP"), "stop_ev": threading.Event(),
                      "thread": None}

    monkeypatch.setattr(
        PipelineRunner, "_desired_blocks",
        lambda self: {"KEEP": _block("KEEP"), "ADD": _block("ADD")})
    r._reconcile()

    assert r.block_ids() == {"KEEP", "ADD"}
    assert fake_ctx == ["ADD"], "이미 돌던 KEEP 을 다시 띄웠다(추적 상태가 날아간다)"


def test_한_지점_구성_실패가_나머지를_멈추지_않는다(monkeypatch):
    r = _make_runner(dynamic=True)

    def _boom(self, b):
        if b["id"] == "BAD":
            raise RuntimeError("스트림 연결 실패 흉내")
        return {"block": b, "stop_ev": threading.Event(), "thread": None}

    monkeypatch.setattr(PipelineRunner, "_build_block_ctx", _boom)
    monkeypatch.setattr(PipelineRunner, "_start_worker", lambda self, bid, c: None)
    monkeypatch.setattr(
        PipelineRunner, "_desired_blocks",
        lambda self: {"BAD": _block("BAD"), "GOOD": _block("GOOD")})

    r._reconcile()

    assert "GOOD" in r.block_ids(), "한 지점 실패가 나머지까지 막았다"
    assert "BAD" not in r.block_ids()


def test_current_blocks가_지금_도는_지점을_돌려준다(fake_ctx, monkeypatch):
    r = _make_runner(dynamic=True)
    monkeypatch.setattr(PipelineRunner, "_desired_blocks",
                        lambda self: {"X": _block("X")})
    r._reconcile()

    blocks = r.current_blocks()
    assert [b["id"] for b in blocks] == ["X"]
    assert blocks[0]["name"] == "X지점"
