"""교통위험 상시 탐지 — 별도 프로세스(traffic-service, API 게이트웨이 Phase 4).

⚠️ 2026-08-31 — 이 파일은 ``service/runner.py::PipelineRunner``에서 교통·
기상 부분만 잘라낸 것이다. `runner.py`는 당분간 그대로 두고(롤백 경로,
계획서 §Phase 4 상세 설계 참고) platform-shell이 계속 불러 쓴다.

침수와 달리 이쪽은 **기존 `Perception`(YOLO+ByteTrack)을 그대로 쓴다** —
교통 판정(정체·속도·정지차량·돌발상황)은 원래부터 추적 결과가 필요했고,
설계 검토에서도 이 부분은 "그대로 이동"으로 확인됐다(설계 사전조사에서
비용이 순증가하는 유일한 지점은 침수·교통이 동시에 켜진 카메라 — 개발
DB 기준 1개 — 뿐이며, 그 카메라는 교통 쪽이 원래 하던 일을 그대로
계속할 뿐이다).
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime

from ..common.config import PROJECT_ROOT, load_config_with_nested_merge
from ..common.notifier import AlertNotifier
from ..core import config_rev
from ..core import evidence as _evidence
from ..core import live_state
from ..core import settings as ug_settings
from ..core import video_quality as _video_quality
from ..traffic_weather.agents.risk_decision import RiskDecisionAgent
from ..traffic_weather.agents.semantic_agent import TRAFFIC_SEMANTIC_DEFAULTS, SemanticAgent
from ..traffic_weather.agents.vlm_situation import VlmSituationAgent
from ..traffic_weather.knowledge.ontology import TRAFFIC_RISK_CATALOG
from ..traffic_weather.perception.detection_source import SyntheticDetectionSource
from ..traffic_weather.perception.perception import Perception
from ..traffic_weather.perception.rainfall_provider import SineRainfallProvider, build_rainfall
from ..traffic_weather.perception.render import frame_to_image, render_scene, to_jpeg_b64
# 순수 함수라 안전하게 재사용한다 — runner.py는 임포트 시점에
# PipelineRunner를 만들지 않는다(main.py가 그렇게 한다).
from .runner import _no_traffic_decision, _notify_decision, _restream_whep_url


def _traffic_snapshot(block, weather, metrics, situation, decision, snap_b64, kind,
                      vehicles, incidents, quality_grade, vlm_src) -> dict:
    """교통위험 카드 1개분 — `runner.py::_snapshot()`의 교통·기상 부분만.

    ⚠️ 필드 이름은 기존과 동일하게 맞춘다 — `static/app.js`의
    `trafficCard()`가 그대로 재사용할 수 있어야 한다.
    """
    objs = sorted(vehicles or [], key=lambda v: -v.speed_drop)[:6]
    _src_cfg = block.get("source") or {}
    _browser_stream_url = _src_cfg.get("origin_url") or _src_cfg.get("url")
    return {
        "block_id": block["id"], "name": block["name"],
        "coordinates": block["coordinates"], "source_kind": kind,
        "stream_url": _browser_stream_url, "stream_type": _src_cfg.get("type"),
        "whep_url": (_restream_whep_url(block["id"])
                    if kind != "restream_unavailable" else None),
        "observed": kind != "restream_unavailable",
        "traffic_enabled": bool(block.get("traffic_enabled", True)),
        "t_sec": metrics.t_sec, "rain_mm_h": weather.rain_mm_h,
        "intensity": weather.intensity.value, "rain_source": weather.source,
        "rain_trend": weather.trend, "rain_delta": weather.delta_mm_h,
        "mean_speed": metrics.mean_speed, "mean_speed_kmh": metrics.mean_speed_kmh,
        "speed_drop": metrics.speed_drop, "state": metrics.state.value,
        "queue_len": metrics.queue_len, "stalled": metrics.stalled,
        "n_vehicles": metrics.n_vehicles,
        "objects": [{"id": v.track_id, "speed": v.speed, "kmh": v.speed_kmh,
                     "drop": v.speed_drop, "stalled": v.stalled} for v in objs],
        "risk_code": decision.risk_code, "risk_name": decision.risk_name,
        "level": decision.level.value, "severity": decision.severity,
        "score": decision.score, "recommendation": decision.recommendation,
        "situation_ko": situation.get("situation_ko", ""),
        "vlm_source": vlm_src, "drivers": decision.drivers, "alert": decision.alert,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "snapshot": snap_b64,
        "incidents": incidents or [],
        "quality_grade": quality_grade,
    }


class TrafficPipelineRunner(threading.Thread):
    """교통위험 상시 탐지 — 지점 하나당 워커 스레드 하나.

    `service/runner.py::PipelineRunner`의 동적 재구성·워커 수명주기
    설계를 그대로 따른다 — 다른 점은 침수·하천이 아예 없다는 것뿐이다.
    """

    def __init__(self, store, blocks, fps: float = 5.0, use_vlm: bool = False,
                 dynamic: bool = False):
        super().__init__(daemon=True)
        self.store = store
        self.fps = fps
        self.dt = 1.0 / fps
        self.use_vlm = use_vlm
        self.dynamic = dynamic
        self._stop_ev = threading.Event()
        self._ctx: dict[str, dict] = {}
        self._ctx_lock = threading.RLock()
        self._workers: list = []
        self._notifier = AlertNotifier()

        self.traffic_risk_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "traffic_risk_config.yaml",
            TRAFFIC_SEMANTIC_DEFAULTS)

        for b in blocks:
            self._ctx[b["id"]] = self._build_block_ctx(b)

    # --- 지점 구성 -------------------------------------------------------

    def _build_block_ctx(self, b: dict) -> dict:
        rp = b.get("rain", {})
        sine = SineRainfallProvider(peak_mm_h=rp.get("peak", 25.0),
                                    period_sec=rp.get("period", 48.0),
                                    phase_sec=rp.get("phase", 0.0))
        try:
            from ..core.db import get_session as _get_settings_session
            _sdb = _get_settings_session()
            try:
                _rain_backend = ug_settings.rainfall_backend(_sdb)
            finally:
                _sdb.close()
        except Exception as e:  # noqa: BLE001
            print(f"[traffic-runner] 전역 설정 조회 실패, sine으로 진행: {str(e)[:120]}")
            _rain_backend = "sine"
        rainfall = build_rainfall(b.get("rainfall"), b["coordinates"],
                                  fallback=sine, default_type=_rain_backend)
        source, baseline_hint, kind = self._build_source(b, rainfall)
        ctx = {
            "block": b, "rainfall": rainfall,
            "frames": source.frames(), "wh": source.frame_wh, "kind": kind,
            "perception": Perception(b, rainfall, fps=self.fps,
                                     baseline_hint=baseline_hint, use_bytetrack=True),
            "vlm": VlmSituationAgent(use_vlm=self.use_vlm, location=b["name"]),
            "semantic": SemanticAgent(self.traffic_risk_cfg),
            "decider": RiskDecisionAgent(TRAFFIC_RISK_CATALOG),
            "last_snap_t": -999.0, "last_b64": None,
            "last_live_state_t": -999.0,
            "frame_no": 0,
            "stop_ev": threading.Event(),
            "thread": None,
        }
        return ctx

    def _build_source(self, block, rainfall):
        """`runner.py::PipelineRunner._build_source()`와 완전히 같다."""
        cfg = block.get("source") or {"type": "synthetic"}
        stype = cfg.get("type", "synthetic")
        if stype in ("video", "rtsp", "hls"):
            target = cfg.get("url") or cfg.get("path")
            restream_swap = bool(cfg.get("origin_url"))
            try:
                from ..core import model_ops
                from ..traffic_weather.perception.detection_source import YoloDetectionSource
                model_key = model_ops.selected_key("traffic") or None
                kwargs = {"fps": self.fps, "loop": True}
                if model_key:
                    kwargs["model"] = model_key
                src = YoloDetectionSource(target, **kwargs)
                print(f"[traffic-runner] {block['id']}: connected YOLO {stype} source "
                      f"'{target}' (model={model_key or 'default'})")
                return src, None, stype
            except Exception as e:  # noqa: BLE001
                if restream_swap:
                    print(f"[traffic-runner] {block['id']}: 재배포 서버 연결 실패, "
                          f"원본으로 전환하지 않습니다(정책) — {str(e)[:90]}")
                    src = SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None)
                    return src, src.base_speed, "restream_unavailable"
                print(f"[traffic-runner] {block['id']}: {stype} '{target}' connection "
                      f"failed -> synthetic fallback ({str(e)[:90]})")
        src = SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None)
        return src, src.base_speed, "synthetic"

    RESTREAM_RETRY_SEC = 300.0

    def _retry_restream_if_due(self, c) -> None:
        if c.get("kind") != "restream_unavailable":
            return
        import time
        now = time.time()
        if now < c.get("next_restream_retry_t", 0.0):
            return
        c["next_restream_retry_t"] = now + self.RESTREAM_RETRY_SEC
        source, baseline_hint, kind = self._build_source(c["block"], c["rainfall"])
        if kind == "restream_unavailable":
            return
        bid = c["block"].get("id", "?")
        print(f"[traffic-runner] {bid}: 재배포 연결 회복 확인 — 실제 판정을 재개합니다.")
        c["frames"] = source.frames()
        c["wh"] = source.frame_wh
        c["kind"] = kind
        c["perception"] = Perception(c["block"], c["rainfall"], fps=self.fps,
                                     baseline_hint=baseline_hint, use_bytetrack=True)

    def _next_frame(self, c):
        try:
            return next(c["frames"])
        except StopIteration:
            src = SyntheticDetectionSource(c["rainfall"], fps=self.fps, duration_sec=None)
            c["frames"] = src.frames()
            c["wh"] = src.frame_wh
            c["kind"] = "synthetic(fallback)"
            c["perception"] = Perception(c["block"], c["rainfall"], fps=self.fps,
                                         baseline_hint=src.base_speed, use_bytetrack=True)
            return next(c["frames"])

    # --- 동적 재구성 -------------------------------------------------------

    def current_blocks(self) -> list[dict]:
        with self._ctx_lock:
            return [c["block"] for c in self._ctx.values()]

    def block_ids(self) -> set[str]:
        with self._ctx_lock:
            return set(self._ctx.keys())

    def traffic_flow_samples(self, camera_id: str
                             ) -> tuple[list[tuple[float, float, float, float]],
                                       tuple[int, int] | None] | None:
        """`runner.py::PipelineRunner.traffic_flow_samples()`와 완전히 같다
        — 통행 방향 자동 생성(Phase 4-A, 교통 로드맵) 화면이 쓴다."""
        with self._ctx_lock:
            c = self._ctx.get(camera_id)
        if c is None:
            return None
        tracker = getattr(c.get("perception"), "tracker", None)
        if tracker is None:
            return None
        try:
            samples = list(getattr(tracker, "flow_samples", None) or [])
        except RuntimeError:  # noqa: BLE001
            samples = []
        return samples, getattr(tracker, "last_frame_wh", None)

    def _start_worker(self, bid: str, c: dict) -> None:
        th = threading.Thread(target=self._block_loop, args=(bid, c), daemon=True)
        c["thread"] = th
        th.start()
        self._workers.append(th)

    def _spawn_block(self, b: dict) -> None:
        bid = b["id"]
        try:
            c = self._build_block_ctx(b)
        except Exception:  # noqa: BLE001
            print(f"[traffic-runner] 신규 지점 구성 실패 id={bid} — 건너뜁니다.")
            traceback.print_exc()
            return
        with self._ctx_lock:
            self._ctx[bid] = c
        self._start_worker(bid, c)
        print(f"[traffic-runner] 신규 지점 상시 탐지 시작 id={bid}")

    def _retire_block(self, bid: str) -> None:
        with self._ctx_lock:
            c = self._ctx.pop(bid, None)
        if c is None:
            return
        c["stop_ev"].set()
        th = c.get("thread")
        if th is not None and th.is_alive():
            th.join(timeout=5.0)
            if th.is_alive():
                print(f"[traffic-runner] 경고 — 지점 {bid} 워커가 5초 안에 끝나지 "
                      "않았습니다(다음 틱에 스스로 빠져나옵니다).")
        try:
            self.store.remove(bid)
        except Exception:  # noqa: BLE001
            pass
        print(f"[traffic-runner] 지점 상시 탐지 중단 id={bid}")

    def _desired_blocks(self) -> dict[str, dict]:
        from ..core import cameras as _cams
        from ..core.db import get_session
        from ..core.roles import Domain as _Dom

        db = get_session()
        try:
            return {b["id"]: b for b in _cams.continuous_blocks(db, _Dom.TRAFFIC.value)}
        finally:
            db.close()

    def _reconcile(self) -> None:
        if not self.dynamic:
            return
        desired = self._desired_blocks()
        current = self.block_ids()
        added = set(desired) - current
        removed = current - set(desired)
        if added or removed:
            print(f"[traffic-runner] 상시 목록 재조정 — 추가 {len(added)}개 / "
                  f"제외 {len(removed)}개 (현재 {len(current)}개)")
        for bid in removed:
            self._retire_block(bid)
        for bid in added:
            self._spawn_block(desired[bid])

    def _watch_config(self) -> None:
        known = config_rev.revision()
        while not self._stop_ev.is_set():
            known = config_rev.wait_change_or_stop(known, 60.0, self._stop_ev)
            if self._stop_ev.is_set():
                return
            try:
                self._reconcile()
            except Exception:  # noqa: BLE001
                print("[traffic-runner] 상시 목록 재조정 실패 — 다음 신호에 다시 봅니다.")
                traceback.print_exc()

    def run(self) -> None:
        with self._ctx_lock:
            items = list(self._ctx.items())
        for bid, c in items:
            self._start_worker(bid, c)
        if self.dynamic:
            threading.Thread(target=self._watch_config, daemon=True).start()
        self._stop_ev.wait()

    def stop(self) -> None:
        self._stop_ev.set()

    # --- 틱 처리 -----------------------------------------------------------

    def _block_loop(self, bid, c) -> None:
        own_stop = c.get("stop_ev")
        while not self._stop_ev.is_set() and not (own_stop and own_stop.is_set()):
            try:
                self._retry_restream_if_due(c)
                t, dets, frame = self._next_frame(c)
                restream_down = (c.get("kind") == "restream_unavailable")
                if frame is not None and not restream_down:
                    _evidence.push(bid, frame)
                ps = c["perception"].step(t, dets, c["wh"])
                weather, metrics = ps.weather, ps.metrics
                img = None
                if t - c["last_snap_t"] >= 1.0 and not restream_down:
                    img = (frame_to_image(frame, dets) if frame is not None
                           else render_scene(dets, weather, c["wh"]))
                    c["last_b64"] = to_jpeg_b64(img)
                    c["last_snap_t"] = t
                if c["block"].get("traffic_enabled") and not restream_down:
                    situation = c["vlm"].interpret(img, metrics, weather,
                                                   location=c["block"]["name"])
                    risk = c["semantic"].infer(weather, metrics, situation,
                                               context={"block": c["block"]["name"]})
                    decision = c["decider"].decide(risk, t)
                    if decision.severity >= ug_settings.traffic_notify_min_severity():
                        _notify_decision(self._notifier, decision, c["block"]["name"])
                else:
                    situation = {}
                    decision = _no_traffic_decision(t)
                if restream_down:
                    vlm_src = "미지정(재배포 서버 응답 없음)"
                elif c["block"].get("traffic_enabled"):
                    vlm_src = c["vlm"].last_source
                else:
                    vlm_src = "미지정(교통 미사용)"
                snap = _traffic_snapshot(
                    c["block"], weather, metrics, situation, decision, c["last_b64"],
                    c["kind"], ps.vehicles, ps.incidents,
                    _video_quality.grade_for(bid), vlm_src)
                self.store.update(bid, snap)
                # ⚠️ 2026-08-31(Phase 4) — 홈 화면(platform-shell)이 이
                # 프로세스의 store를 더는 직접 못 읽으므로 DB에도 남긴다.
                # 1초로 눌러 쓴다(이 루프는 최대 5fps로 돈다) —
                # runner_flood.py와 같은 이유.
                if t - c["last_live_state_t"] >= 1.0:
                    c["last_live_state_t"] = t
                    level = decision.level.value if c["block"].get(
                        "traffic_enabled") else ""
                    # ⚠️ 실기 확인(2026-08-31) — 처음에 `and not restream_down`
                    # 을 넣었다가 실사용 중 발견: 그러면 재배포 서버가 잠깐
                    # 끊긴 카메라(흔한 일 — YoloDetectionSource가 기동
                    # 시점에 MediaMTX 경로가 아직 안 올라와 있으면 곧바로
                    # "재배포 서버 연결 실패"로 떨어진다)가 홈 화면 "지점
                    # N개소"에서 통째로 빠져, CCTV 관리(S-80) 화면의 "상시
                    # N개소"보다 항상 작게 보이는 결함이 됐다(실사용자 제보로
                    # 발견). 예전 `_traffic_summary()`도 `traffic_enabled`만
                    # 봤지 연결 상태는 안 봤다 — 그 동작을 그대로 되살린다.
                    # `available`은 "이 도메인으로 지정됐는가"만 뜻한다
                    # (core/live_state.py 모듈 docstring과도 일치).
                    live_state.upsert(
                        bid, "traffic",
                        level=level,
                        is_available=bool(c["block"].get("traffic_enabled", True)))
            except Exception as e:  # noqa: BLE001
                try:
                    if not c.get("err_traced"):
                        c["err_traced"] = True
                        print(f"[traffic-runner:{bid}] 첫 오류 — 아래 트레이스백은 "
                              f"이 지점에서 한 번만 남깁니다.")
                        traceback.print_exc()
                    print(f"[traffic-runner:{bid}] {str(e)[:120]}")
                except Exception:  # noqa: BLE001
                    print(f"[traffic-runner:{bid}] error (log message encoding failed)")
            self._stop_ev.wait(self.dt)
