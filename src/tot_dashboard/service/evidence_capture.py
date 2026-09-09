"""증거 수집 연결 — 탐지 훅과 파일 저장 워커 (S-88).

:mod:`..core.evidence` 는 프레임을 담고 파일을 쓸 줄만 안다. **언제 담기
시작할지**(등급 정책)와 **언제 파일로 굳힐지**(뒤 구간 대기)는 여기서 정한다.

흐름
    1. 탐지가 이벤트로 올라감 → :func:`on_detection` 이 등급을 보고 수집 시작
    2. 정지영상은 **그 자리에서** 남긴다 — 클립이 실패해도 증거는 남는다
    3. 뒤 구간(10초)이 지나면 워커가 클립을 파일로 쓰고 DB에 기록

**탐지 스레드에서 파일을 쓰지 않는다.** 인코딩은 수백 ms가 걸릴 수 있고,
그동안 그 지점의 탐지가 멈춘다.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from ..core import audit
from ..core import evidence as EV
from ..core import settings as ug_settings
from ..core.db import get_session
from ..core.models import EventEvidence

log = logging.getLogger("urbanguard.evidence")

# 워커가 「끝난 수집이 있나」를 확인하는 주기.
TICK_SEC = 2.0

# 보존기간 지난 자료를 확인하는 주기. 자주 볼 필요가 없다 — 개월 단위 기준에
# 한 시간 오차는 의미가 없고, 매 틱마다 전체를 훑으면 DB만 괴롭힌다.
PURGE_INTERVAL_SEC = 3600.0

# 한 번에 지우는 최대 건수. 오래 방치된 서버에서 수만 건이 한꺼번에 걸리면
# 트랜잭션이 길어져 다른 작업을 막는다. 남으면 다음 주기에 마저 지운다.
PURGE_BATCH = 500

EVIDENCE_PURGE = "evidence.purge"


def on_detection(ev, *, is_new: bool) -> None:
    """이벤트가 새로 생기거나 등급이 올랐을 때 호출된다(코어 훅).

    **DB 세션을 새로 열지 않는다** — 부르는 쪽 트랜잭션 한가운데다. 여기서
    커밋하면 아직 확정되지 않은 이벤트가 함께 커밋된다. 파일 쓰기와 DB 기록은
    전부 워커로 넘긴다.
    """
    level = ev.level or ""
    if level not in ug_settings.evidence_levels():
        return
    cam = ev.block_id or ""
    if not cam:
        return
    # 정지영상은 지금 남긴다. 링 버퍼의 마지막 화면이 곧 「탐지된 그 순간」이다.
    blob = EV.latest_frame(cam)
    # ★ **같은 순간의 탐지 상자**를 함께 가져온다. 여기서 못 가져오면 나중에
    #   다시 물어도 다른 틱의 값이라 「탐지된 그 순간」과 어긋난다.
    box_info = EV.latest_boxes(cam)
    boxes, fw, fh = box_info if box_info else ([], 0, 0)
    started = EV.start(ev.id, cam, level, domain=ev.domain or "",
                       boxes=boxes, frame_w=fw, frame_h=fh)
    if blob is None and not started:
        return
    _queue_still(ev.id, cam, ev.domain or "", level, blob,
                boxes=boxes, frame_w=fw, frame_h=fh)
    if not started:
        log.debug("증거 클립 수집을 시작하지 못했습니다 event=%s", ev.id)


# 정지영상 기록 대기열. 훅은 트랜잭션 안이라 DB를 못 만지므로 워커에 넘긴다.
_still_q: list[tuple] = []
_q_lock = threading.Lock()


def _queue_still(event_id: int, cam: str, domain: str, level: str,
                 blob: bytes | None, *,
                 boxes: list[dict] | None = None,
                 frame_w: int = 0, frame_h: int = 0) -> None:
    if blob is None:
        return
    with _q_lock:
        _still_q.append((event_id, cam, domain, level, blob,
                         boxes or [], frame_w, frame_h))


def _record(db, *, event_id: int, cam: str, domain: str, level: str,
            kind: str, saved: tuple[str, int, str], duration: int = 0,
            boxes: list[dict] | None = None,
            frame_w: int = 0, frame_h: int = 0) -> None:
    rel, size, digest = saved
    db.add(EventEvidence(event_id=event_id, camera_id=cam, domain=domain,
                         level=level, kind=kind, path=rel, bytes=size,
                         sha256=digest, duration_sec=duration,
                         # ⚠️ 상자가 없으면 **빈 리스트가 아니라 None** 으로
                         #    남긴다 — 「이번엔 없었다」와 「이 자료는 상자
                         #    개념이 없다」를 구분해야 한다(routes_evidence.py
                         #    의 has_box 참고). 빈 리스트도 「없다」로 보이지만
                         #    한쪽은 도메인이 상자를 아예 안 주는 경우다.
                         boxes=(boxes or None), frame_w=(frame_w or None),
                         frame_h=(frame_h or None)))


class EvidenceWorker(threading.Thread):
    """파일 쓰기 담당. 탐지 스레드를 막지 않기 위해 따로 돈다."""

    def __init__(self):
        super().__init__(daemon=True, name="urbanguard-evidence")
        self._stop = threading.Event()
        # 기동 직후 한 번은 바로 확인한다 — 꺼져 있던 동안 보존기간이 지난
        # 자료가 쌓여 있을 수 있다.
        self._next_purge = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("증거 수집 워커 시작 (앞 %d초 · 뒤 %d초 · %dfps)",
                 EV.PRE_SEC, EV.POST_SEC, EV.FPS)
        while not self._stop.is_set():
            try:
                self._flush_stills()
                self._finish_clips()
                self._purge_expired()
            except Exception as e:  # noqa: BLE001
                # 증거 수집이 죽어도 관제는 계속돼야 한다.
                log.warning("증거 워커 오류(계속 진행): %s", str(e)[:200])
            self._stop.wait(TICK_SEC)

    def _purge_expired(self) -> None:
        """보존기간이 지난 증거 자료를 지운다.

        **개인정보를 목적 없이 계속 들고 있지 않기 위한 장치다.** 기간은
        기관이 정하며(:data:`~.settings.KEY_EVIDENCE_RETENTION_MONTHS`),
        0이면 아무것도 하지 않는다.

        지운 사실은 **감사 로그에 남긴다** — 사람이 지운 것이 아니어도
        「누가 지웠나」에 답할 수 있어야 한다.
        """
        now = time.monotonic()
        if now < self._next_purge:
            return
        self._next_purge = now + PURGE_INTERVAL_SEC

        months = ug_settings.evidence_retention_months()
        if months <= 0:
            return
        cutoff = EV.months_ago(months)

        db = get_session()
        try:
            rows = list(db.scalars(
                select(EventEvidence)
                .where(EventEvidence.captured_at < cutoff)
                .limit(PURGE_BATCH)).all())
            if not rows:
                return
            size = 0
            for r in rows:
                EV.remove_file(r.path)
                size += r.bytes or 0
            db.execute(sa_delete(EventEvidence)
                       .where(EventEvidence.id.in_([r.id for r in rows])))
            audit.record(db, action=EVIDENCE_PURGE, login_id="system",
                         target=f"보존기간 경과 자동 파기 {len(rows)}건",
                         after={"months": months,
                                "cutoff": cutoff.isoformat(),
                                "count": len(rows), "bytes": size})
            db.commit()
            log.info("증거 자료 자동 파기 %d건 (%s) — 보존기간 %d개월",
                     len(rows), EV.human_size(size), months)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.warning("증거 자동 파기 실패: %s", str(e)[:200])
        finally:
            db.close()

    def _flush_stills(self) -> None:
        with _q_lock:
            items, _still_q[:] = list(_still_q), []
        if not items:
            return
        db = get_session()
        try:
            for (event_id, cam, domain, level, blob,
                boxes, frame_w, frame_h) in items:
                saved = EV.write_image(event_id, blob)
                if saved:
                    _record(db, event_id=event_id, cam=cam, domain=domain,
                            level=level, kind=EV.KIND_IMAGE, saved=saved,
                            boxes=boxes, frame_w=frame_w, frame_h=frame_h)
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.warning("증거 정지영상 기록 실패: %s", str(e)[:200])
        finally:
            db.close()

    def _finish_clips(self) -> None:
        for event_id in EV.due():
            job = EV.pop(event_id)
            if job is None:
                continue
            saved = EV.write_clip(event_id, job.frames, when=job.started)
            if saved is None:
                log.info("증거 클립을 남기지 못했습니다 event=%s (정지영상은 있습니다)",
                         event_id)
                continue
            duration = max(int(len(job.frames) / max(EV.FPS, 1)), 1)
            db = get_session()
            try:
                _record(db, event_id=event_id, cam=job.camera_id,
                        domain=job.domain, level=job.level, kind=EV.KIND_CLIP,
                        saved=saved, duration=duration,
                        boxes=job.boxes, frame_w=job.frame_w,
                        frame_h=job.frame_h)
                db.commit()
                log.info("증거 클립 저장 event=%s %s (%s)",
                         event_id, saved[0], EV.human_size(saved[1]))
            except Exception as e:  # noqa: BLE001
                db.rollback()
                # 파일은 썼는데 DB 기록이 실패하면 **주인 없는 파일**이 남는다.
                # 목적 없는 개인정보 보관이므로 지운다.
                EV.remove_file(saved[0])
                log.warning("증거 클립 기록 실패(파일 삭제함) event=%s: %s",
                            event_id, str(e)[:200])
            finally:
                db.close()
