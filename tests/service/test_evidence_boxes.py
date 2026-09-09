"""증거 자료 — 이벤트가 난 위치(boxes) 저장 (S-88, 2026-08-20).

## 왜 이 시험들이 있나

「탐지 상자를 저장해 달라」는 요청을 받았다. **지어낼 수 없는 것**이라
도메인마다 실제로 관측된 값만 골랐다 — 침수는 물 마스크의 경계, 인파는
그 이벤트를 일으킨 트랙의 추적 상자, 노면은 YOLO 가 찾은 손상 상자.

## ⚠️ 만들면서 실제로 결함을 하나 더 찾았다

**클립 증거의 `domain` 이 항상 빈 문자열이었다.** 정지영상은 이벤트에서
`domain` 을 받는데, 클립은 `_finish_clips` 가 `""` 를 그대로 썼다 — 그 결과
**도메인 필터·배지가 클립에는 전혀 안 먹혔다.** 이 시험 파일에서 함께 잡는다.
"""
from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import evidence as EV
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, Event, EventEvidence
from tot_dashboard.service import evidence_capture as EC

PFX = "TEST-EVB-"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(EventEvidence).where(
            EventEvidence.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema, tmp_path, monkeypatch):
    monkeypatch.setattr(EV, "EVIDENCE_DIR", tmp_path / "evidence")
    EV.reset()
    _purge()
    yield
    EV.reset()
    _purge()


def _frame(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)


# --- push_boxes / latest_boxes ----------------------------------------------


def test_상자를_기억했다가_그대로_돌려준다():
    EV.push_boxes("CAM1", [{"x1": 1, "y1": 2, "x2": 3, "y2": 4, "label": "물"}],
                  (100, 200))
    boxes, w, h = EV.latest_boxes("CAM1")
    assert boxes == [{"x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0, "label": "물"}]
    assert (w, h) == (100, 200)


def test_한_번도_없으면_None이다():
    """★ `None` 과 `([], w, h)` 를 구분한다 — 「도메인이 상자를 아예 안
    준다」와 「이번 틱엔 없었다」는 다른 사실이다."""
    assert EV.latest_boxes("CAM-NEVER-PUSHED") is None


def test_빈_리스트를_넘기면_낡은_상자를_지운다():
    """⚠️ 다음 이벤트에 이전 상자가 잘못 붙으면 안 된다."""
    EV.push_boxes("CAM2", [{"x1": 0, "y1": 0, "x2": 1, "y2": 1, "label": "x"}],
                  (10, 10))
    EV.push_boxes("CAM2", [], (10, 10))
    boxes, _, _ = EV.latest_boxes("CAM2")
    assert boxes == []


def test_reset은_상자_캐시도_비운다():
    EV.push_boxes("CAM3", [{"x1": 0, "y1": 0, "x2": 1, "y2": 1, "label": "x"}],
                  (10, 10))
    EV.reset()
    assert EV.latest_boxes("CAM3") is None


def test_예외가_밖으로_안_나간다():
    """증거 수집이 탐지를 막으면 안 된다 — 이상한 값을 줘도 조용히 넘긴다."""
    EV.push_boxes("CAM4", [{"x1": "안숫자"}], (10, 10))  # 키 누락·타입 오류
    # 여기까지 예외 없이 왔으면 충분하다.


# --- start() 가 domain/boxes 를 Job 에 담는다 -------------------------------


def test_start가_도메인과_상자를_Job에_담는다():
    ok = EV.start(90001, "CAM5", "경계", domain="road",
                 boxes=[{"x1": 1, "y1": 1, "x2": 2, "y2": 2, "label": "포트홀"}],
                 frame_w=640, frame_h=480)
    assert ok is True
    job = EV.pop(90001)
    assert job.domain == "road"
    assert job.boxes == [{"x1": 1, "y1": 1, "x2": 2, "y2": 2, "label": "포트홀"}]
    assert (job.frame_w, job.frame_h) == (640, 480)


def test_start의_기본값은_빈_상자다():
    ok = EV.start(90002, "CAM6", "경계")
    assert ok is True
    job = EV.pop(90002)
    assert job.domain == ""
    assert job.boxes == []


# --- ⚠️ 회귀: 클립의 domain 이 이제 채워진다 --------------------------------


def test_클립_증거의_domain이_이벤트와_같다():
    """★ 예전에는 이 값이 늘 `""` 였다. `EV.start()` 가 담아 준 도메인을
    `_record()` 가 실제로 쓰는지 확인한다.

    ⚠️ **공유 큐·백그라운드 워커 스레드는 일부러 거치지 않는다.** 이 파일이
    단독으로는 통과하는데 전체 스위트에서만 실패한 적이 있다 — 원인은 다른
    시험 파일의 `TestClient(app)` 가 기동시킨 **진짜 `EvidenceWorker`
    백그라운드 스레드**가 전역 큐(`EV._still_q`)와 전역 `EV._jobs` 를
    이 시험과 동시에 건드려, 그 스레드가 뒤늦게 쓰려는 순간 이벤트 행이
    이미 다른 시험의 정리 과정에서 지워져 있었기 때문이다(외래키 위반).
    **이 시험이 증명하려는 것은 `_record()` 의 도메인 전달이지, 큐잉·스레드
    타이밍이 아니다** — 그래서 `_record()` 를 직접 불러 결정적으로 검사한다.
    """
    cam = f"{PFX}A"
    db = get_session()
    try:
        db.add(Camera(id=cam, name="시험지점", lat=35.1, lng=129.0))
        ev = Event(domain="crowd", block_id=cam, place_name="시험지점",
                   event_type="시험", level="경계", peak_level="경계",
                   status="open")
        db.add(ev)
        db.commit()
        ev_id = ev.id

        boxes = [{"x1": 5, "y1": 5, "x2": 15, "y2": 15, "label": "배회"}]
        blob = EV._encode(_frame(1), 80)
        img_saved = EV.write_image(ev_id, blob)
        EC._record(db, event_id=ev_id, cam=cam, domain="crowd", level="경계",
                  kind=EV.KIND_IMAGE, saved=img_saved,
                  boxes=boxes, frame_w=80, frame_h=60)

        frames = [(float(i), EV._encode(_frame(i), 80)) for i in range(3)]
        clip_saved = EV.write_clip(ev_id, frames)
        # ★ 이게 이번에 잡은 회귀다 — 예전에는 이 자리가 `domain=""` 였다.
        EC._record(db, event_id=ev_id, cam=cam, domain="crowd", level="경계",
                  kind=EV.KIND_CLIP, saved=clip_saved, duration=2,
                  boxes=boxes, frame_w=80, frame_h=60)
        db.commit()

        rows = {r.kind: r for r in db.query(EventEvidence)
                .filter(EventEvidence.event_id == ev_id).all()}
        assert "image" in rows and "clip" in rows
        assert rows["clip"].domain == "crowd"
        assert rows["image"].domain == "crowd"
        assert rows["image"].boxes == boxes
        assert rows["clip"].boxes == boxes
        assert rows["clip"].frame_w == 80 and rows["clip"].frame_h == 60
    finally:
        db.close()


def test_상자가_없으면_None으로_저장된다():
    """⚠️ 빈 리스트가 아니라 `None` — 「이번엔 없었다」와 「상자 개념이
    아예 없다」를 구분한다. (위 시험과 같은 이유로 `_record()` 를 직접 부른다.)
    """
    cam = f"{PFX}B"
    db = get_session()
    try:
        db.add(Camera(id=cam, name="시험지점2", lat=35.1, lng=129.0))
        ev = Event(domain="flood", block_id=cam, place_name="시험지점2",
                   event_type="시험", level="경계", peak_level="경계",
                   status="open")
        db.add(ev)
        db.commit()
        ev_id = ev.id

        blob = EV._encode(_frame(9), 80)
        img_saved = EV.write_image(ev_id, blob)
        EC._record(db, event_id=ev_id, cam=cam, domain="flood", level="경계",
                  kind=EV.KIND_IMAGE, saved=img_saved)
        db.commit()

        row = (db.query(EventEvidence)
               .filter(EventEvidence.event_id == ev_id, EventEvidence.kind == "image")
               .one())
        assert row.boxes is None
        assert row.frame_w is None and row.frame_h is None
    finally:
        db.close()
