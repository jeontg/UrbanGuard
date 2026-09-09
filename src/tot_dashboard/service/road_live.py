"""노면 실시간 관제 — 지점 집중 감시 (S-44).

상시 순회(``continuous.RoadContinuousWatcher``)는 지점당 **15분 주기**다.
포트홀·균열은 분 단위로 변하지 않으니 그 주기로 충분하다.

그런데 노면 도메인에는 **분 단위로 변하는 상황**도 있다.

* 결빙·적설 — 기온이 떨어지는 30분 사이에 노면이 바뀐다
* 낙하물·사고 잔해 — 발생과 동시에 위험하고, 치우면 사라진다
* 침수와 겹치는 구간 — 수위가 오르내리는 동안 노면 상태가 계속 달라진다

15분 전 결과로는 이런 상황에 대응할 수 없다. 그래서 관제요원이 **한 지점을
골라 짧은 주기로 반복 관측**하도록 한다. 상시 순회를 15분에서 1분으로 낮추는
방식을 쓰지 않은 이유는 아래와 같다.

한 번에 한 지점만
    ``RoadDefectAnalyzer.analyze`` 는 내부 락으로 직렬화된다. 여러 지점을
    동시에 짧은 주기로 돌리면 서로 락을 뺏느라 **상시 순회까지 굶는다.**
    집중 감시는 「지금 이 지점이 급하다」는 선언이므로 하나면 된다.

자동 종료 시한
    켜 놓고 잊으면 CPU를 계속 문다. 기본 30분 뒤 스스로 멈추고, 필요하면
    다시 켠다. 무기한 감시가 필요한 지점이라면 S-80에서 상시 탐지로 지정하는
    것이 옳다.

결과는 상시 순회와 **같은 저장소**(``road.results``)에 쌓인다. 화면이 두 경로를
구분해 읽을 필요가 없고, 이력도 한곳에 모인다.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("urbanguard.road_live")

# 집중 감시 주기. 1회 관측이 15초 걸리고 추론 시간이 더해지므로 30초보다
# 짧게 두면 쉬는 구간 없이 계속 분석하게 된다.
FOCUS_PERIOD_SEC = float(os.environ.get("URBANGUARD_ROAD_FOCUS_PERIOD", "60"))
FOCUS_MIN_PERIOD_SEC = 30.0
FOCUS_MAX_PERIOD_SEC = 600.0
# 1회 관측 길이. 상시 순회·선택 탐지와 같은 기준을 쓴다.
FOCUS_DURATION_SEC = float(os.environ.get("URBANGUARD_ROAD_FOCUS_DURATION", "15"))
# 자동 종료 시한. 켜 두고 잊는 것을 막는다.
FOCUS_TTL_SEC = float(os.environ.get("URBANGUARD_ROAD_FOCUS_TTL", "1800"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_revision() -> int:
    from ..core import config_rev
    return config_rev.revision()


def _still_designated(camera_id: str) -> bool:
    """이 지점이 아직 노면 탐지 대상인가.

    S-80에서 「도로 노면 관리」를 꺼도 감시가 계속 돌면, 대상에서 뺀 지점을
    분석기가 붙잡고 있게 된다. DB를 읽지 못하면 **참으로 본다** — 조회 실패로
    운영자가 켜 둔 감시를 끊는 것이 더 나쁘다.
    """
    try:
        from ..core import cameras as C
        from ..core import roles as R
        from ..core.db import get_session
        db = get_session()
        try:
            cam = C.get(db, camera_id)
            if cam is None:
                return False
            row = cam.domain_row(R.Domain.ROAD.value)
            return bool(row and row.enabled)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        log.exception("노면 대상 확인 실패 camera=%s", camera_id)
        return True


class RoadFocusWatcher(threading.Thread):
    """지점 하나를 짧은 주기로 반복 관측한다."""

    def __init__(self, analyzer, camera_id: str, camera_name: str, block: dict,
                 period_sec: float = FOCUS_PERIOD_SEC,
                 duration_sec: float = FOCUS_DURATION_SEC,
                 ttl_sec: float = FOCUS_TTL_SEC):
        super().__init__(daemon=True, name=f"road-focus-{camera_id}")
        self.analyzer = analyzer
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.block = block
        self.period_sec = min(max(period_sec, FOCUS_MIN_PERIOD_SEC),
                              FOCUS_MAX_PERIOD_SEC)
        self.duration_sec = duration_sec
        self.ttl_sec = max(ttl_sec, self.period_sec)
        self.started_at = time.time()
        self.rounds = 0
        self.last_note = ""
        self._stop_ev = threading.Event()
        # 왜 멈췄는지 화면에 남긴다. 시한이 다 돼 스스로 멈춘 것과 운영자가
        # 끈 것을 구분하지 못하면 「왜 갱신이 멈췄지」를 알 수 없다.
        self.stop_reason = ""
        # 설정이 바뀌었을 때만 대상 지정 여부를 다시 확인한다(매 바퀴 DB를
        # 두드릴 이유가 없다).
        self._rev = _config_revision()

    # -- 조회 --------------------------------------------------------------
    @property
    def expires_at(self) -> float:
        return self.started_at + self.ttl_sec

    def remaining_sec(self) -> float:
        return max(self.expires_at - time.time(), 0.0)

    def status(self) -> dict:
        return {
            "running": self.is_alive() and not self._stop_ev.is_set(),
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "period_sec": self.period_sec,
            "duration_sec": self.duration_sec,
            "rounds": self.rounds,
            "remaining_sec": round(self.remaining_sec()),
            "ttl_sec": self.ttl_sec,
            "last_note": self.last_note,
            "stop_reason": self.stop_reason,
        }

    # -- 실행 --------------------------------------------------------------
    def run(self) -> None:
        from ..road import dataset_collector
        from ..road import results as road_results

        from . import event_sync

        # 집중 감시도 학습 프레임을 남긴다. 주기가 60초로 짧지만 수집기가
        # 지점당 최소 간격을 지키므로 같은 장면이 연달아 쌓이지는 않는다.
        sink = dataset_collector.sink(self.camera_id, self.camera_name,
                                      source="focus")

        log.info("노면 집중 감시 시작 — %s(%s), %.0f초 주기, %.0f분 시한",
                 self.camera_name, self.camera_id, self.period_sec,
                 self.ttl_sec / 60.0)
        while not self._stop_ev.is_set():
            started = time.time()
            try:
                res = self.analyzer.analyze(
                    mode="cctv", target=self.camera_id,
                    duration_sec=self.duration_sec, block=self.block,
                    frame_sink=sink)
                d = res.to_dict() if hasattr(res, "to_dict") else {}
                self.rounds += 1
                self.last_note = d.get("note") or ""
                # 실패도 그대로 기록한다. 프레임을 못 받은 사실 자체가
                # 「지금 이 지점을 못 보고 있다」는 정보다.
                road_results.record(self.camera_id, d, source="focus")
                event_sync.record_road_result(d)
            except Exception:  # noqa: BLE001
                log.exception("노면 집중 감시 오류 camera=%s", self.camera_id)
                self.last_note = "분석 중 오류가 발생했습니다."
            # 시한 검사는 **한 바퀴를 돌고 나서** 한다. 앞에 두면 시한이 짧을 때
            # 한 번도 관측하지 않고 끝나, 운영자가 켠 감시가 아무것도 남기지
            # 않는다 — 그럴 바에는 켜지 말았어야 한다.
            if self.remaining_sec() <= 0:
                self.stop_reason = "감시 시한이 끝나 자동으로 멈췄습니다."
                log.info("노면 집중 감시 시한 종료 — %s", self.camera_id)
                break
            # S-80에서 이 지점의 「도로 노면 관리」를 껐으면 감시도 멈춘다.
            # 설정에서 뺀 지점을 분석기가 계속 붙잡고 있으면 안 된다.
            rev = _config_revision()
            if rev != self._rev:
                self._rev = rev
                if not _still_designated(self.camera_id):
                    self.stop_reason = ("이 지점이 노면 탐지 대상에서 "
                                        "해제되어 감시를 멈췄습니다.")
                    log.info("노면 집중 감시 대상 해제 — %s", self.camera_id)
                    break
            # 관측에 걸린 시간을 빼고 쉰다. 빼지 않으면 실제 주기가
            # 「설정 주기 + 관측 시간」으로 늘어난다.
            rest = max(self.period_sec - (time.time() - started), 1.0)
            self._stop_ev.wait(rest)
        if not self.stop_reason:
            self.stop_reason = "운영자가 감시를 중지했습니다."

    def stop(self) -> None:
        self._stop_ev.set()


class RoadFocusManager:
    """집중 감시를 **한 건만** 유지한다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._watcher: RoadFocusWatcher | None = None
        # 마지막으로 끝난 감시. 화면에서 「왜 멈췄는지」를 보여 준다.
        self._last: dict | None = None

    def start(self, analyzer, camera_id: str, camera_name: str, block: dict,
              period_sec: float = FOCUS_PERIOD_SEC,
              ttl_sec: float = FOCUS_TTL_SEC) -> dict:
        """감시를 시작한다. 이미 다른 지점을 보고 있으면 그쪽을 먼저 멈춘다."""
        with self._lock:
            prev = self._watcher
            if prev is not None and prev.is_alive():
                prev.stop_reason = f"「{camera_name}」 감시로 전환했습니다."
                prev.stop()
                self._last = prev.status()
            w = RoadFocusWatcher(analyzer, camera_id, camera_name, block,
                                 period_sec=period_sec, ttl_sec=ttl_sec)
            self._watcher = w
            w.start()
            return w.status()

    def stop(self, reason: str = "운영자가 감시를 중지했습니다.") -> dict | None:
        with self._lock:
            w = self._watcher
            if w is None:
                return None
            w.stop_reason = reason
            w.stop()
            self._last = w.status()
            self._watcher = None
            return self._last

    def status(self) -> dict:
        """현재 감시 상태. 감시 중이 아니면 마지막 감시 기록을 함께 준다."""
        with self._lock:
            w = self._watcher
            if w is not None and w.is_alive():
                return {"active": True, **w.status()}
            # 시한이 끝나 스레드가 스스로 죽은 경우를 여기서 정리한다.
            if w is not None:
                self._last = w.status()
                self._watcher = None
            last = dict(self._last) if self._last else None
            return {"active": False, "camera_id": None, "last": last}


# 서비스 전역에 하나. 분석기가 하나뿐이므로 감시도 하나다.
manager = RoadFocusManager()
