"""AI 모델 학습 실행 (core/training_jobs.py) — 신규, 2026-08-25.

지켜야 할 것.

* **동시 1건만** — GPU가 없어 전부 CPU 학습이다. 두 도메인을 동시에
  돌리면 서로 느려지기만 한다
* **학습 데이터가 없으면 시작 자체를 거부한다** — 데이터 없이 「학습했다」는
  결과를 내면 안 된다(교통위험·인파관리는 아직 데이터가 없다)
* **재기동으로 핸들을 잃어도 사실대로 적는다** — 성공·실패를 추측하지 않고
  "결과를 알 수 없다"고 표시한다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import training_jobs as TJ
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import TrainingRun


class _FakeUser:
    id = None
    name = "테스트관리자"
    login_id = "tester"
    dept = ""


class _FakePopen:
    """실제 프로세스를 띄우지 않고 subprocess.Popen 흉내만 낸다."""

    def __init__(self, *a, **kw):
        self.pid = 999001
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = -15  # SIGTERM 상당


@pytest.fixture
def db(db_schema):
    s = get_session()
    s.execute(sa_delete(TrainingRun))
    s.commit()
    TJ._PROCS.clear()
    yield s
    s.execute(sa_delete(TrainingRun))
    s.commit()
    TJ._PROCS.clear()
    s.close()


@pytest.fixture(autouse=True)
def _no_real_dataset(monkeypatch, tmp_path):
    """flood 데이터셋만 "있다"고 만들고, 나머지는 실제 보관소 상태 그대로 둔다.

    ⚠️ traffic·crowd 는 실제로 데이터가 없는 것이 **이 기능 자체가 확인하려는
    사실**이라 건드리지 않는다 — 그래야 "데이터 없으면 거부"가 실제 운영과
    같은 조건에서 검증된다.
    """
    flood_dir = tmp_path / "flood"
    (flood_dir / "images").mkdir(parents=True)
    (flood_dir / "labels").mkdir()

    orig_resolve = TJ.resolve_dataset
    monkeypatch.setattr(TJ, "resolve_dataset", lambda key: (
        flood_dir if key == "flood_labeled" else orig_resolve(key)))


@pytest.fixture(autouse=True)
def _fake_subprocess(monkeypatch):
    monkeypatch.setattr(TJ.subprocess, "Popen", _FakePopen)


def test_데이터가_있으면_학습을_시작한다(db):
    run, err = TJ.start(db, "flood", {"epochs": 5}, _FakeUser())
    assert err == ""
    assert run is not None
    assert run.status == "running"
    assert run.pid == 999001


def test_데이터가_없으면_거부한다(db):
    """교통위험은 2026-08-25 확인 기준 자체 학습 데이터가 없다."""
    run, err = TJ.start(db, "traffic", {}, _FakeUser())
    assert run is None
    assert "학습 데이터가 없습니다" in err


def test_동시_1건만_허용한다(db):
    run1, err1 = TJ.start(db, "flood", {}, _FakeUser())
    assert run1 is not None
    run2, err2 = TJ.start(db, "flood", {}, _FakeUser())
    assert run2 is None
    assert "이미 다른 학습이 실행 중입니다" in err2


def test_다른_도메인이어도_동시_실행은_막는다(db):
    """CPU 전용 환경 — 도메인이 달라도 예외를 두지 않는다."""
    run1, _ = TJ.start(db, "flood", {}, _FakeUser())
    assert run1 is not None
    # road 는 실제 보관소에 데이터가 있어(2026-08-14 이전 상태) 데이터
    # 부족이 아니라 "이미 실행 중" 사유로 막혀야 한다.
    run2, err2 = TJ.start(db, "road", {}, _FakeUser())
    assert run2 is None
    assert "실행 중" in err2


def test_실행이_끝나면_다음_학습을_시작할_수_있다(db):
    run1, _ = TJ.start(db, "flood", {}, _FakeUser())
    proc = TJ._PROCS[run1.id]
    proc._rc = 0  # 정상 종료로 시뮬레이션
    busy = TJ.active_run(db)  # 내부적으로 _reconcile_running 을 돌린다
    assert busy is None
    run2, err2 = TJ.start(db, "flood", {}, _FakeUser())
    assert err2 == ""
    assert run2 is not None


def test_실패로_종료되면_상태가_failed다(db):
    run1, _ = TJ.start(db, "flood", {}, _FakeUser())
    proc = TJ._PROCS[run1.id]
    proc._rc = 1
    TJ.active_run(db)
    db.refresh(run1)
    assert run1.status == "failed"
    assert "종료 코드 1" in run1.error


def test_중지하면_stopped_상태가_된다(db):
    run1, _ = TJ.start(db, "flood", {}, _FakeUser())
    ok, err = TJ.stop(db, run1.id)
    assert ok, err
    db.refresh(run1)
    assert run1.status == "stopped"
    assert run1.id not in TJ._PROCS


def test_이미_끝난_실행은_다시_중지할_수_없다(db):
    run1, _ = TJ.start(db, "flood", {}, _FakeUser())
    TJ.stop(db, run1.id)
    ok, err = TJ.stop(db, run1.id)
    assert not ok
    assert "이미 끝난" in err


def test_서버_재기동으로_핸들을_잃으면_결과를_알_수_없다고_적는다(db):
    """_PROCS 를 비워 "서버가 재기동된 뒤" 상황을 흉내낸다."""
    run1, _ = TJ.start(db, "flood", {}, _FakeUser())
    TJ._PROCS.pop(run1.id)  # 핸들만 사라짐(재기동 시뮬레이션)
    TJ.active_run(db)
    db.refresh(run1)
    assert run1.status == "stopped"
    assert "재기동" in run1.error


def test_침수_학습_기본_인자가_스크립트_호출에_담긴다():
    args = TJ._flood_args({"epochs": 10, "arch": "deeplabv3", "size": 512,
                           "batch": 4}, "test_run")
    assert "--epochs" in args and "10" in args
    assert "--arch" in args and "deeplabv3" in args
    assert "--name" in args and "test_run" in args


def test_road_학습_인자에_데이터셋_선택이_담긴다():
    args = TJ._road_args({"dataset": "svrdd", "epochs": 12}, "r1")
    assert "--dataset" in args and "svrdd" in args


# --- progress_percent() (2026-08-25 신설) -----------------------------------
#
# 다른 도메인 탭에 "OOO 도메인 진행률 OO%"만 간단히 보여주려고 만들었다
# (사용자 요청 — 인파관리 탭인데 도로 노면 전체 로그가 그대로 보이는 게
# 어색하다는 지적). 침수·인파는 완료된 에폭마다 한 줄만 찍고, 도로·교통
# (ultralytics YOLO)은 배치마다 다시 그려 에폭 분수와 그 안의 진행률을
# 함께 찍는다 — 두 형식을 각각 확인한다.

def _run_with_log(tmp_path, text: str) -> TrainingRun:
    p = tmp_path / "sample.log"
    p.write_text(text, encoding="utf-8")
    run = TrainingRun(domain="flood", status="running", log_path=str(p))
    return run


def test_침수_인파_형식의_에폭_진행률을_읽는다(tmp_path):
    run = _run_with_log(tmp_path,
        "[train] epoch 1/40  loss 0.84  val IoU 0.28  F1 0.43\n"
        "[train] epoch 3/40  loss 0.70  val IoU 0.31  F1 0.46\n")
    p = TJ.progress_percent(run)
    assert p == {"epoch": 3, "total_epochs": 40, "percent": round(3 / 40 * 100)}


def test_YOLO_형식의_에폭과_배치_진행률을_함께_읽는다(tmp_path):
    # 실제 로그에서 확인한 형태를 재현 — \x1b[K 로 줄을 지우고 다시 그린다.
    run = _run_with_log(tmp_path,
        "\x1b[K       2/15         0G      3.061      3.419      2.275         25"
        "        416: 37% ━━━ 59/160 20.4s/it 16:53<34:18\n")
    p = TJ.progress_percent(run)
    assert p["epoch"] == 2 and p["total_epochs"] == 15
    # (에폭-1 + 배치비율) / 전체 * 100 = (1 + 0.37) / 15 * 100 ≈ 9%
    assert p["percent"] == round((1 + 0.37) / 15 * 100)


def test_로그가_없으면_None을_돌려준다(tmp_path):
    run = TrainingRun(domain="flood", status="running", log_path=None)
    assert TJ.progress_percent(run) is None


def test_로그에_에폭_패턴이_없으면_None을_돌려준다(tmp_path):
    run = _run_with_log(tmp_path, "그냥 아무 텍스트\n초기화 중...\n")
    assert TJ.progress_percent(run) is None
