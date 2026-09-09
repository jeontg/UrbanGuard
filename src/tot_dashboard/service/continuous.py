"""인파·노면 상시 탐지 워처.

침수는 ``runner.PipelineRunner`` 가 카메라마다 스레드를 띄워 초 단위로 계속
분석한다. 인파·노면은 검출 비용이 훨씬 커서 같은 방식으로는 CPU가 버티지
못하므로, **도메인마다 다른 주기**로 돌린다.

===========  ==========================================  ====================
도메인       방식                                        기본 주기
===========  ==========================================  ====================
침수         카메라별 스레드, 연속 분석                  ~5 fps (runner.py)
인파         카메라별 스레드, 표본 추출                  카메라당 5초
노면         단일 스레드가 카메라를 순회                 카메라당 15분
===========  ==========================================  ====================

인파를 카메라별 스레드로 둔 이유 — 추적(ByteTrack)과 배회 판정은 **끊기지 않는
시간축**을 전제로 한다. 한 스레드가 카메라를 번갈아 보면 추적 ID가 매번
끊겨 배회를 영영 잡지 못한다. 노면은 반대로 정적인 대상이라 순회로 충분하고,
분석 1회가 15초씩 걸려 동시 실행이 오히려 해롭다.

대상은 DB(``camera_domains.continuous``)가 정한다. 관리자가 S-80에서 켜고 끈다.

반영 시점이 도메인마다 다르다
    **노면은 즉시 반영된다.** S-80·S-81을 저장하면 ``core/config_rev`` 가
    신호를 보내고, 자고 있던 순회 스레드가 깨어나 목록을 다시 읽는다.
    정적인 대상을 순회하는 방식이라 목록을 갈아끼워도 잃을 상태가 없다.

    **인파는 서비스 재시작 시점이다.** 카메라마다 스레드와 추적기(ByteTrack)를
    두는데, 실행 중에 갈아끼우면 추적 ID와 배회 타이머가 초기화된다. 배회는
    「같은 사람이 N초 이상 머물렀는가」로 판정하므로, 타이머가 끊기면 그 지점의
    배회를 한동안 잡지 못한다. 목록을 즉시 반영하는 대가로 탐지를 놓치는 셈이라
    그렇게 하지 않았다.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from ..core import evidence as _evidence

log = logging.getLogger("urbanguard.continuous")

# 증거 팝업(S-88)에 쓰는 한글 이름. behavior_events.EVENT_LOITERING/
# INTRUSION/FALL·abandoned_object.EVENT_ABANDONED_OBJECT 값(영문)을 그대로
# 보여 주면 관제요원이 못 읽는다.
_CROWD_EVENT_LABEL = {"Loitering": "배회", "Intrusion": "침입", "Fall": "쓰러짐 의심",
                     "AbandonedObject": "위험물 투기 의심"}

# 인파: 카메라 한 대를 몇 초 간격으로 표본 추출할지.
# 1초로 두면 검출이 못 따라가 큐가 밀린다(실측: 검출 1회에 수 초).
CROWD_INTERVAL_SEC = float(os.environ.get("URBANGUARD_CROWD_INTERVAL", "5"))
# 노면: 한 카메라를 다시 분석하기까지의 간격. 포트홀은 분 단위로 변하지 않는다.
ROAD_PERIOD_SEC = float(os.environ.get("URBANGUARD_ROAD_PERIOD", "900"))
# 노면 1회 관측 길이. 선택 탐지(15초)와 같은 기준을 쓴다.
ROAD_DURATION_SEC = float(os.environ.get("URBANGUARD_ROAD_DURATION", "15"))
# 기동 유예. 서비스가 뜨는 순간에는 침수 파이프라인이 카메라 수만큼 스트림을
# 열고 검출 모델도 함께 올라와, 이때 스트림을 잡으려 하면 프레임을 못 받는다
# (실측: 기동 직후 노면 첫 순회가 「프레임을 받지 못했습니다」로 끝났다).
CROWD_START_DELAY_SEC = float(os.environ.get("URBANGUARD_CROWD_START_DELAY", "20"))
ROAD_START_DELAY_SEC = float(os.environ.get("URBANGUARD_ROAD_START_DELAY", "60"))


def continuous_cameras(domain: str) -> list:
    """해당 도메인에서 **상시 탐지로 지정된** 카메라. DB 실패 시 빈 목록."""
    try:
        from ..core import cameras as C
        from ..core.db import get_session
        db = get_session()
        try:
            return list(C.for_domain(db, domain, continuous=True))
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        log.exception("상시 탐지 대상을 읽지 못했습니다 domain=%s", domain)
        return []


def _stream_url(cam) -> str:
    if getattr(cam, "source_type", "") == "hls":
        url = getattr(cam, "source_url", "") or ""
        # ★ 2026-08-28 — CCTV 재배포 허브(MediaMTX). 인파는 `core/cameras.py::
        # to_block_dict()`를 거치지 않고 여기서 `Camera` ORM을 직접 읽으므로,
        # 그 함수의 재배포 치환을 그대로 못 쓴다 — 같은 판단을 여기서
        # 별도로 한다.
        #
        # ⚠️ **캐시만 보지 않는다** — `to_block_dict()`가 실사용 점검에서
        # 겪은 것과 같은 이유(설정 캐시가 아직 안 채워진 시점에 다른
        # 프로세스가 이미 DB에 켜 둔 값을 못 읽는 문제, 위 파일 주석 참고)
        # 로 세션을 새로 연다. 인파 상시 워처는 카메라당 이 함수를 딱
        # 1회(기동 시점)만 부르므로 비용은 무시할 만하다.
        try:
            from ..core import restream as _restream
            from ..core import settings as _ug_settings
            from ..core.db import get_session as _get_settings_session
            db = _get_settings_session()
            try:
                cid = getattr(cam, "id", "")
                if (_ug_settings.restream_enabled(db)
                        and not _restream.is_excluded(cid, db)):
                    url = _restream.rtsp_url(cid, db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            log.exception("재배포 설정 조회 실패, 원본 URL로 진행 camera=%s",
                         getattr(cam, "id", "?"))
        return url
    if getattr(cam, "source_type", "") == "video":
        return getattr(cam, "source_path", "") or ""
    return ""


# --- 인파 -------------------------------------------------------------------
class CrowdContinuousWatcher:
    """상시 지정된 카메라마다 워커 스레드를 띄워 인파를 계속 관측한다."""

    def __init__(self, interval_sec: float = CROWD_INTERVAL_SEC,
                 start_delay_sec: float = CROWD_START_DELAY_SEC):
        self.interval_sec = max(interval_sec, 0.5)
        self.start_delay_sec = max(start_delay_sec, 0.0)
        self._stop_ev = threading.Event()
        self._threads: list[threading.Thread] = []
        self._targets: list[tuple[str, str]] = []   # (id, name)

    @property
    def target_count(self) -> int:
        return len(self._targets)

    def start(self) -> None:
        from ..core import cameras as C

        cams = continuous_cameras("crowd")
        for cam in cams:
            url = _stream_url(cam)
            if not url:
                log.warning("인파 상시 탐지 제외 — 영상 소스가 없습니다 camera=%s", cam.id)
                continue
            # 카메라마다 별도 분석기를 둔다. 하나를 공유하면 추적 ID와 배회
            # 타이머가 지점 간에 뒤섞인다.
            analyzer = self._build_analyzer(C.to_block_dict(cam))
            if analyzer is None:
                continue
            self._targets.append((cam.id, cam.name))
            th = threading.Thread(target=self._loop, name=f"crowd-{cam.id}",
                                  args=(cam.id, cam.name, url, analyzer), daemon=True)
            th.start()
            self._threads.append(th)
        if self._targets:
            log.info("인파 상시 탐지 시작 — %d개소, %.1f초 간격",
                     len(self._targets), self.interval_sec)

    @staticmethod
    def _build_analyzer(block: dict):
        try:
            from ..crowd.live_analyzer import CrowdLiveAnalyzer, mock_restricted_roi
            cfg = dict(block.get("crowd") or {})
            # 카메라별 명시 설정(``crowd.source.type``)이 있으면 그 값이
            # 항상 이긴다. 없을 때만 전역 기본값(S-95,
            # core.settings.crowd_continuous_source)을 쓴다 — 카메라마다
            # JSON을 고치지 않고도 관리자가 화면 설정 하나로 실측(detector)
            # /합성(mock) 전체를 바꿀 수 있게 하려는 것이다(2026-08-27,
            # 인파 상시 카메라별 모니터링 신설). DB 조회 실패 시 안전한
            # 기본값(detector)으로 fail-open — rainfall_provider와 같은
            # 원칙(runner.py::_build_block_ctx 참고).
            if not cfg.get("source"):
                try:
                    from ..core import settings as ug_settings
                    from ..core.db import get_session as _get_settings_session
                    _sdb = _get_settings_session()
                    try:
                        _source_type = ug_settings.crowd_continuous_source(_sdb)
                    finally:
                        _sdb.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("인파 상시 검출 소스 설정 조회 실패, "
                               "detector로 진행: %s", str(e)[:120])
                    _source_type = "detector"
                cfg["source"] = {"type": _source_type}
            # ★ 2026-08-29 — 타일 격자도 같은 원칙(전역 기본값, 카메라별
            # 명시 설정이 있으면 그 값이 이긴다). detector가 아니면 안
            # 쓰이는 값이라 조회 자체를 건너뛴다.
            if cfg["source"].get("type") == "detector" and not cfg["source"].get("tile_grid"):
                try:
                    from ..core import settings as ug_settings
                    from ..core.db import get_session as _get_settings_session
                    _sdb = _get_settings_session()
                    try:
                        cfg["source"]["tile_grid"] = list(ug_settings.crowd_tile_grid_tuple(_sdb))
                    finally:
                        _sdb.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("인파 타일 격자 설정 조회 실패, 2x2로 진행: %s", str(e)[:120])
            cfg.setdefault("intrusion_roi", mock_restricted_roi())
            cfg.setdefault("loiter_sec", 60.0)
            cfg.setdefault("commercial_zone", True)
            return CrowdLiveAnalyzer(cfg=cfg, block_id=block.get("id"),
                                     node_id=(block.get("source") or {}).get("cctv_name"),
                                     fps=1.0 / max(CROWD_INTERVAL_SEC, 0.5))
        except Exception:  # noqa: BLE001
            log.exception("인파 분석기를 만들지 못했습니다 block=%s", block.get("id"))
            return None

    def _loop(self, cam_id: str, cam_name: str, url: str, analyzer) -> None:
        try:
            import cv2
        except Exception:  # noqa: BLE001
            log.exception("영상 라이브러리를 쓸 수 없어 인파 상시 탐지를 중단합니다")
            return

        from . import event_sync

        # 기동 직후 혼잡을 피해 잠시 기다렸다 붙는다.
        if self._stop_ev.wait(self.start_delay_sec):
            return

        from ..common import stream_guard

        t0 = time.time()
        cap = None
        next_sample = 0.0
        backoff = stream_guard.Backoff(start_sec=self.interval_sec * 2)
        while not self._stop_ev.is_set():
            try:
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                    # ⚠️ 호스트가 풀리지 않으면 FFmpeg 를 부르지 않는다.
                    # 재접속을 반복하며 네이티브 컨텍스트를 쌓다가 프로세스가
                    # 통째로 죽은 적이 있다(2026-08-12 세그폴트).
                    if not stream_guard.host_reachable(url):
                        backoff.sleep(self._stop_ev)
                        continue
                    cap = cv2.VideoCapture(url)
                    if not cap.isOpened():
                        # 스트림이 죽어 있으면 잠시 쉬었다 다시 붙는다.
                        backoff.sleep(self._stop_ev)
                        continue
                    backoff.success()
                ok, frame = cap.read()
                if not ok or frame is None:
                    cap.release()
                    cap = None
                    self._stop_ev.wait(self.interval_sec)
                    continue

                elapsed = time.time() - t0
                if elapsed < next_sample:
                    continue
                next_sample = elapsed + self.interval_sec

                # 증거 링 버퍼(S-88) — 인파 이벤트도 직전 장면이 필요하다.
                _evidence.push(cam_id, frame)
                snap = analyzer.step(elapsed, frame)
                d = snap.to_dict() if hasattr(snap, "to_dict") else {}
                # ★ 증거 팝업이 「이벤트가 발생한 부분」에 쓸 값(S-88, 2026-08-20).
                #   **이 이벤트를 일으킨 그 사람의 실제 추적 상자**만 담는다 —
                #   화면에 잡힌 사람 전부가 아니라, 배회·침입을 실제로 일으킨
                #   트랙만이라야 「여기서 났다」는 말이 진실이 된다.
                boxes = [{"x1": b[0], "y1": b[1], "x2": b[2], "y2": b[3],
                         "label": _CROWD_EVENT_LABEL.get(e.get("eventType"), "")}
                        for e in (d.get("events") or [])
                        if (b := e.get("bbox"))]
                _evidence.push_boxes(cam_id, boxes,
                                     (frame.shape[1], frame.shape[0]))
                event_sync.record_crowd_snapshot({
                    "block_id": cam_id, "node_id": cam_name,
                    "events": d.get("events") or [],
                    "people_count": d.get("person_count"),
                    "source": d.get("source"),
                })
                # 흐름 이력. 지금까지 계산해 놓고 버리던 값이라 **평상시
                # 기준선이 없었다** — 예측은 과거가 있어야 가능하다.
                event_sync.record_crowd_observation(cam_id, cam_name, d)
            except Exception:  # noqa: BLE001
                log.exception("인파 상시 탐지 오류 camera=%s", cam_id)
                self._stop_ev.wait(self.interval_sec)
        if cap is not None:
            cap.release()

    def stop(self) -> None:
        self._stop_ev.set()

    def status(self) -> dict:
        return {"running": bool(self._threads) and not self._stop_ev.is_set(),
                "targets": [{"id": i, "name": n} for i, n in self._targets],
                "interval_sec": self.interval_sec}


# --- 노면 -------------------------------------------------------------------
class RoadContinuousWatcher(threading.Thread):
    """상시 지정된 노면 카메라를 순회하며 주기적으로 분석한다.

    분석기가 하나뿐이고 1회에 15초가 걸리므로 **직렬로** 돈다. 카메라가 N대면
    한 바퀴에 최소 N×15초가 걸리고, 그 뒤 다음 주기까지 쉰다.

    대상 목록은 **매 바퀴 다시 읽고, S-80·S-81 저장 시에는 즉시 다시 읽는다.**
    예전에는 기동 시점에 한 번만 읽어서, 관리자가 「상시」로 지정해도 서비스를
    다시 띄우기 전에는 아무도 그 지점을 보지 않았다 — 화면에는 「상시」로 뜨는데
    실제로는 관측되지 않는 상태였고, 그러면 운영자가 설정을 믿을 수 없게 된다.
    """

    def __init__(self, analyzer, period_sec: float = ROAD_PERIOD_SEC,
                 duration_sec: float = ROAD_DURATION_SEC,
                 start_delay_sec: float = ROAD_START_DELAY_SEC):
        super().__init__(daemon=True, name="road-continuous")
        self.analyzer = analyzer
        self.period_sec = max(period_sec, 30.0)
        self.duration_sec = duration_sec
        self.start_delay_sec = max(start_delay_sec, 0.0)
        self._stop_ev = threading.Event()
        self._targets: list[tuple[str, str]] = []
        self._last_round: list[dict] = []
        self._rev = -1          # 마지막으로 목록을 읽은 설정 리비전
        self._reloads = 0
        # 진행 상황 — 순회가 직렬이라 한 바퀴에 N×15초가 걸린다. 지금 어디를
        # 보고 있는지가 안 보이면 운영자는 멈춘 것과 도는 중인 것을 구분할 수
        # 없다(15분 동안 화면이 그대로이므로).
        self._current: dict | None = None
        self._round_no = 0
        self._resume_at = 0.0   # 쉬는 중이면 다음 순회 시작 예정 시각

    def _load_targets(self) -> list[tuple[str, str, dict]]:
        """상시 지정된 노면 카메라를 DB에서 다시 읽는다.

        리비전을 **읽기 전에** 기록한다. 읽는 도중에 설정이 바뀌면 그 변경을
        놓치지 않고 다음 바퀴에 다시 읽게 된다(반대로 하면 놓친다).
        """
        from ..core import cameras as C
        from ..core import config_rev

        rev = config_rev.revision()
        cams = [c for c in continuous_cameras("road") if _stream_url(c)]
        blocks = [(c.id, c.name, C.to_block_dict(c)) for c in cams]
        before = {t[0] for t in self._targets}
        after = {b[0] for b in blocks}
        if self._rev >= 0 and before != after:
            log.info("노면 상시 대상 변경 — 추가 %s / 제외 %s",
                     sorted(after - before) or "없음", sorted(before - after) or "없음")
        if self._rev >= 0:
            self._reloads += 1
        self._rev = rev
        self._targets = [(b[0], b[1]) for b in blocks]
        return blocks

    def run(self) -> None:
        from ..core import config_rev
        from ..road import results as road_results

        from . import event_sync

        log.info("노면 상시 탐지 시작 — %.0f초 주기", self.period_sec)
        # 기동 직후 혼잡을 피한다. 여기서 바로 붙으면 첫 순회가 통째로 헛돈다.
        if self._stop_ev.wait(self.start_delay_sec):
            return

        while not self._stop_ev.is_set():
            started = time.time()
            blocks = self._load_targets()
            self._round_no += 1
            total = len(blocks)
            round_out: list[dict] = []
            for idx, (cam_id, cam_name, block) in enumerate(blocks, start=1):
                if self._stop_ev.is_set():
                    break
                # 방금 본 지점은 건너뛴다. 설정이 바뀔 때마다 목록을 다시 읽는데
                # 그때마다 전부 다시 돌리면, 관리자가 몇 군데를 연달아 고치는
                # 동안 같은 카메라를 계속 붙잡고 있게 된다.
                if self._recently_seen(cam_id):
                    continue
                # 지금 어느 지점을 보고 있는지 화면에 알린다. 순회가 직렬이라
                # 한 바퀴에 N×15초가 걸리는데, 진행 상황이 안 보이면 운영자는
                # 「멈춘 것인지 도는 중인지」를 구분할 수 없다.
                self._current = {"id": cam_id, "name": cam_name,
                                 "index": idx, "total": total,
                                 "started_at": time.time()}
                try:
                    d = self._analyze_once(cam_id, block)
                    round_out.append({"id": cam_id, "name": cam_name,
                                      "defects": len(d.get("defects") or []),
                                      "note": d.get("note") or ""})
                    # 노면 현황 화면이 읽을 수 있게 남긴다. 남기지 않으면
                    # 화면은 모의값밖에 보여 줄 것이 없다.
                    road_results.record(cam_id, d, source="continuous")
                    event_sync.record_road_result(d)
                except Exception:  # noqa: BLE001
                    log.exception("노면 상시 탐지 오류 camera=%s", cam_id)
                finally:
                    self._current = None
            if round_out:
                self._last_round = round_out

            # 대상이 없으면 짧게 확인한다 — 관리자가 방금 지정했을 수 있는데
            # 15분을 기다리면 「켰는데 아무 일도 없다」로 보인다.
            rest = (30.0 if not blocks
                    else max(self.period_sec - (time.time() - started), 30.0))
            self._resume_at = time.time() + rest
            # ⚠️ ``_stop_ev.wait`` 만 쓰면 설정이 바뀌어도 최대 15분을 잔다.
            # 변경 신호가 오면 그 자리에서 깨어나 목록을 다시 읽는다.
            config_rev.wait_change_or_stop(self._rev, rest, self._stop_ev)
            self._resume_at = 0.0

    def _recently_seen(self, cam_id: str) -> bool:
        """직전 관측이 주기의 절반 안쪽이면 아직 볼 때가 아니다."""
        from ..road import results as road_results

        r = road_results.get(cam_id)
        if r is None:
            return False
        age = road_results.age_seconds(r.get("analyzed_at"))
        return age is not None and age < self.period_sec / 2

    def _analyze_once(self, cam_id: str, block: dict) -> dict:
        """한 카메라를 분석한다. 프레임을 못 받으면 한 번만 다시 시도한다.

        HLS는 다른 분석이 몰린 순간에 프레임을 못 주는 일이 있다. 다음 순회까지
        기다리면 15분을 통째로 날리므로, 짧게 쉬었다 한 번 더 붙어 본다.

        관측한 프레임은 학습 데이터 수집기로도 넘긴다 — 어차피 뽑은 프레임이라
        스트림을 다시 열지 않아도 되고, 시간대·날씨가 자연히 섞인다. 수집이
        꺼져 있으면 수집기가 즉시 되돌려 준다.
        """
        from ..road import dataset_collector

        sink = dataset_collector.sink(cam_id, block.get("name", cam_id),
                                      source="continuous")
        for attempt in (1, 2):
            res = self.analyzer.analyze(mode="cctv", target=cam_id,
                                        duration_sec=self.duration_sec, block=block,
                                        frame_sink=sink)
            d = res.to_dict() if hasattr(res, "to_dict") else {}
            # ★ 증거 영상(S-88). ⚠️ **예전에는 노면 지점의 프레임을 아예
            #   증거 링 버퍼에 넘기지 않았다**(2026-08-20 발견) — 인파는
            #   `_loop` 에서 매 틱 `_evidence.push` 를 부르는데, 노면 상시
            #   순회에는 그 호출이 처음부터 없었다. 그 결과 **노면 이벤트는
            #   증거(정지영상·클립)가 한 번도 남을 수 없는 구조**였다.
            #   여기서 함께 고친다 — 15초 관측에서 실제로 손상이 잡힌
            #   대표 프레임과 그 프레임의 손상 상자만 넘긴다(다른 프레임의
            #   손상과 섞으면 상자와 화면이 서로 다른 순간의 것이 된다).
            frame = getattr(res, "evidence_frame", None)
            if frame is not None:
                _evidence.push(cam_id, frame)
                boxes = getattr(res, "evidence_boxes", None) or []
                _evidence.push_boxes(cam_id, boxes,
                                     (frame.shape[1], frame.shape[0]))
            if d.get("frames_analyzed") or attempt == 2:
                return d
            log.info("노면 상시 탐지 재시도 camera=%s (%s)", cam_id, d.get("note") or "")
            if self._stop_ev.wait(5.0):
                return d
        return d

    def stop(self) -> None:
        self._stop_ev.set()

    def status(self) -> dict:
        # 지역 변수로 받아 둔다 — 읽는 사이에 순회 스레드가 비울 수 있다.
        cur = self._current
        current = None
        if cur:
            current = {**cur,
                       "elapsed_sec": round(max(time.time() - cur["started_at"], 0))}
        resume = self._resume_at
        return {"running": self.is_alive() and not self._stop_ev.is_set(),
                "targets": [{"id": i, "name": n} for i, n in self._targets],
                "target_count": len(self._targets),
                "period_sec": self.period_sec,
                # 지금 보고 있는 지점(없으면 쉬는 중)과 다음 순회까지 남은 시간.
                "current": current,
                "round": self._round_no,
                "resume_in_sec": (round(max(resume - time.time(), 0))
                                  if resume and current is None else None),
                # 화면이 「설정은 상시인데 아직 안 잡힌 지점」을 가려낼 수 있게
                # 워처가 실제로 들고 있는 목록과 리비전을 함께 내려준다.
                "config_rev": self._rev,
                "reloads": self._reloads,
                "last_round": self._last_round}
