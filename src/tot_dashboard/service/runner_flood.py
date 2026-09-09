"""침수 상시 탐지 — 별도 프로세스(flood-service, API 게이트웨이 Phase 4).

⚠️ 2026-08-31 — 이 파일은 ``service/runner.py::PipelineRunner``에서 침수·
하천 부분만 잘라낸 것이다. `runner.py`는 당분간 그대로 두고(롤백 경로,
계획서 §Phase 4 상세 설계 "진행 순서" 참고) platform-shell이 계속
불러 쓴다 — 실제 트래픽이 이쪽으로 넘어와 검증된 뒤에야 그쪽을 지운다.

**왜 ``Perception``(YOLO+ByteTrack)을 안 쓰는가**
    설계 검토로 확인한 사실 — `flood_metrics_engine.py::update()`는
    차량·사람의 **원본 박스 위치**만 읽고, 추적 ID·속도·정지판정은 전혀
    읽지 않는다. `Perception`을 통째로 들여오면 이 프로세스가 안 쓰는
    ByteTrack까지 다시 돈다 — 원본 미추적 검출(`YoloDetectionSource`가
    돌려주는 ``Detection``)만 있으면 충분하다. 사람/차량을 가르는 로직은
    ``traffic_weather.perception.perception.Perception.step()``의 것과
    똑같다(같은 ``PERSON_CLASSES`` 기준) — 다만 여기서는 그 뒤 ByteTrack
    갱신을 하지 않고 그대로 쓴다.

**``stopped_vehicles_near_water``를 단순화한 이유**
    예전에는 "정지(ByteTrack `.stalled`) + 물 근처"의 결합값이었다.
    `.stalled`는 침수 자체 점수(`flood_risk_score`)에도 안 쓰이는 표시
    전용 필드였고(코드로 확인), 이 값을 유지하려면 교통 서비스와 실시간
    으로 교차조회해야 하는데 그건 "도메인별 독립"이라는 이번 단계의
    목적과 충돌한다. 그래서 "이 서비스 자신이 감지한 차량 박스가 물
    마스크와 겹치는가"만으로 판정한다(정지 여부는 안 본다).

**침수 전용 카메라의 연산량은 오히려 준다** — 예전 구조는 침수만 켠
카메라도 ByteTrack까지 돌리고 있었다(실측 확인). 겹치는 카메라(개발 DB
기준 1개)만 디코딩·추론이 순증가할 뿐, 전체로는 늘지 않거나 준다 —
계획서 §Phase 4 "왜 완전 이중화인가" 참고.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime

from ..common.config import PROJECT_ROOT, load_config_with_nested_merge
from ..common.models_loader import resolve_device
from ..common.notifier import AlertNotifier
from ..common.roi import load_roi_for_camera
from ..core import config_rev
from ..core import evidence as _evidence
from ..core import live_state
from ..core import settings as ug_settings
from ..core import video_quality as _video_quality
from ..flood.flood_metrics_engine import FloodMetricsEngine
from ..flood.metrics_core import bottom_center, point_on_mask
from ..flood.risk_engine import RiskEngine, RiskPredictor, write_back
from ..flood.river_ontology import FLOOD_RIVER_CATALOG
from ..flood.river_provider import build_river
from ..flood.river_risk import FloodRiverAgent
from ..flood.water_segmentation import (WaterResult, is_likely_corrupted_frame,
                                        mask_bbox, segment_water,
                                        segment_without_letterbox)
from ..models import WeatherIntensity
from ..traffic_weather.agents.risk_decision import RiskDecisionAgent
from ..traffic_weather.perception.detection_source import (PERSON_CLASSES,
                                                            SyntheticDetectionSource)
from ..traffic_weather.perception.rainfall_provider import (SineRainfallProvider,
                                                             build_rainfall)
from ..traffic_weather.perception.render import frame_to_image, render_scene, to_jpeg_b64
# 순수 함수라 안전하게 재사용한다(§도입부 — 이 모듈이 main.py를 통째로
# 불러오는 게 아니라 이 함수들만 골라 임포트한다는 뜻. runner.py 자체는
# 임포트 시점에 PipelineRunner를 만들지 않는다 — main.py가 그렇게 한다).
from .runner import (_RISK_DEFAULTS, _WATER_DEFAULTS, _depth_of, _flood_model_rel,
                     _notify_flood_risk, _restream_whep_url)

_RIVER_CATALOG = FLOOD_RIVER_CATALOG


def _notify_river(notifier: AlertNotifier, decision, block_name: str) -> None:
    """하천수위 판정(FR_*)을 알린다 — `runner.py::_notify_decision`과 같은
    모양이지만, 그쪽은 교통(TWR_*)·하천(FR_*)을 함께 받던 공용 함수였다.
    이 프로세스엔 교통이 없으므로 하천 전용으로 이름만 좁혀 둔다(동작은
    동일 — event_key 네임스페이스가 이미 갈라져 있어 안전하다)."""
    from .runner import _notify_decision
    _notify_decision(notifier, decision, block_name)


def _split_detections(detections) -> tuple[list, list[tuple[float, float]]]:
    """원본 검출을 차량 박스 / 사람 발밑점으로 가른다.

    ``traffic_weather.perception.perception.Perception.step()``과 완전히
    같은 기준(``PERSON_CLASSES``)을 쓴다 — 다만 그 뒤 ByteTrack 갱신은
    하지 않는다(이 프로세스는 추적이 필요 없다).
    """
    vehicles = [d for d in detections if d.cls not in PERSON_CLASSES]
    persons = [bottom_center(d.bbox) for d in detections if d.cls in PERSON_CLASSES]
    return vehicles, persons


def _flood_snapshot(block, t_sec, weather, river, river_decision, snap_b64, kind,
                    flood, corrupted_skips: int, quality_grade) -> dict:
    """침수 카드 1개분 — `runner.py::_snapshot()`의 침수·하천 부분만.

    ⚠️ 필드 이름은 기존과 동일하게 맞춘다 — `static/app.js`의
    `floodCard()`가 그대로 재사용할 수 있어야 프런트 변경 범위가
    "SSE 소스가 둘로 나뉜다"는 사실 하나로 국한된다.
    """
    _src_cfg = block.get("source") or {}
    _browser_stream_url = _src_cfg.get("origin_url") or _src_cfg.get("url")
    snap = {
        "block_id": block["id"], "name": block["name"],
        "coordinates": block["coordinates"], "source_kind": kind,
        "stream_url": _browser_stream_url, "stream_type": _src_cfg.get("type"),
        "whep_url": (_restream_whep_url(block["id"])
                    if kind != "restream_unavailable" else None),
        "observed": kind != "restream_unavailable",
        "t_sec": t_sec,
        "rain_mm_h": weather.rain_mm_h, "intensity": weather.intensity.value,
        "rain_source": weather.source, "rain_trend": weather.trend,
        "rain_delta": weather.delta_mm_h,
        "river_level": round(river.level_m, 2) if river else None,
        "river_status": river.status.value if river else None,
        "river_source": river.source if river else None,
        "river_risk_code": river_decision.risk_code if river_decision else "",
        "river_risk_name": river_decision.risk_name if river_decision else "",
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "snapshot": snap_b64,
        "flood_corrupted_skips": corrupted_skips,
        "quality_grade": quality_grade,
    }
    if flood is not None:
        m, risk, pred, stopped_near_water = flood
        snap.update({
            "water_available": True,
            "water_area_ratio": round(m.water_area_ratio, 4),
            "water_expansion_rate": round(m.water_expansion_rate, 5),
            "water_roi_defined": m.roi_defined,
            "water_near_low_point": m.water_near_low_point,
            "water_crosses_lane": m.water_crosses_lane,
            "vehicles_tire_in_water": m.vehicles_tire_in_water,
            "persons_in_danger": m.persons_in_danger,
            "stopped_vehicles_near_water": stopped_near_water,
            "flood_risk_score": risk.risk_score,
            "flood_risk_grade": risk.risk_grade,
            "flood_risk_grade_label": risk.grade_label,
            "flood_risk_top_reason": risk.top_reason,
            "flood_risk_components": risk.components,
            "flood_pred_trend": pred.trend_label,
            "flood_pred_confidence": pred.confidence,
            "flood_pred_ratio_10s": round(pred.pred_ratio.get(10, m.water_area_ratio), 4),
            "flood_pred_risk_10s": pred.pred_risk.get(10, risk.risk_score),
            "flood_eta_danger_sec": pred.eta.get("danger"),
            "flood_eta_shutdown_sec": pred.eta.get("shutdown"),
        })
        depth, level, saturated = _depth_of(block["id"], m.water_area_ratio)
        snap.update({"flood_depth_cm": depth, "flood_depth_level": level,
                    "flood_depth_saturated": saturated})
    else:
        snap["water_available"] = False
    return snap


class FloodPipelineRunner(threading.Thread):
    """침수 상시 탐지 — 지점 하나당 워커 스레드 하나.

    `service/runner.py::PipelineRunner`의 동적 재구성(``config_rev``
    구독)·워커 수명주기 설계를 그대로 따른다 — 다른 점은 교통(Perception/
    ByteTrack/VLM/semantic/decider)이 아예 없다는 것뿐이다.
    """

    def __init__(self, store, blocks, fps: float = 5.0, dynamic: bool = False):
        super().__init__(daemon=True)
        self.store = store
        self.fps = fps
        self.dt = 1.0 / fps
        self.dynamic = dynamic
        self._stop_ev = threading.Event()
        self._ctx: dict[str, dict] = {}
        self._ctx_lock = threading.RLock()
        self._workers: list = []
        self._notifier = AlertNotifier()

        self.risk_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "risk_config.yaml", _RISK_DEFAULTS)
        self.water_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "water_config.yaml", _WATER_DEFAULTS)
        self._water_device = resolve_device(self.water_cfg.get("device", "auto"))
        self._water_model_path = PROJECT_ROOT / _flood_model_rel(
            self.water_cfg["water_model_path"])
        self._water_model_shared = None
        self._water_model_cached = False
        self._water_backend = self.water_cfg.get("water_backend", "auto").strip()
        self._water_backend_effective = ""
        self._water_model_used = None
        self._water_load_error = ""
        self._water_tv_model_path = PROJECT_ROOT / self.water_cfg["water_tv_model_path"]
        self._water_tv_conf = float(self.water_cfg.get("water_tv_conf", 0.50))

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
            print(f"[flood-runner] 전역 설정 조회 실패, sine으로 진행: {str(e)[:120]}")
            _rain_backend = "sine"
        rainfall = build_rainfall(b.get("rainfall"), b["coordinates"],
                                  fallback=sine, default_type=_rain_backend)
        river = build_river(b.get("river"))
        source, kind = self._build_source(b, rainfall)
        roi = load_roi_for_camera(
            b["id"], "flood",
            fallback_path=PROJECT_ROOT / "configs" / "roi" / f"{b['id']}.json")
        ctx = {
            "block": b, "rainfall": rainfall, "river": river,
            "frames": source.frames(), "wh": source.frame_wh, "kind": kind,
            "river_agent": FloodRiverAgent(),
            "river_decider": RiskDecisionAgent(_RIVER_CATALOG),
            "last_snap_t": -999.0, "last_b64": None,
            "last_live_state_t": -999.0,
            "water_model": self._load_water_model() if kind in ("hls", "rtsp", "video") else None,
            "roi": roi, "roi_rev": config_rev.revision(),
            "flood_engine": FloodMetricsEngine(roi=roi),
            "risk_engine": RiskEngine(self.risk_cfg, self.risk_cfg),
            "risk_predictor": None,
            "last_water_t": -999.0, "last_flood": None,
            "frame_no": 0,
            "stop_ev": threading.Event(),
            "thread": None,
        }
        ctx["risk_predictor"] = RiskPredictor(ctx["risk_engine"], self.risk_cfg)
        return ctx

    def _build_source(self, block, rainfall):
        """`runner.py::PipelineRunner._build_source()`와 같은 로직이다.

        ⚠️ ``SyntheticDetectionSource``는 합성 차량 속도를 만들 때도
        ``rainfall.at(t)``를 내부적으로 부른다(강수량에 따라 정체
        시나리오를 만듦) — 침수 판정 자체는 이 신호를 안 쓰지만, 합성원
        생성자 자체는 유효한 rainfall 객체가 있어야 한다(``None``을 넘기면
        첫 프레임 생성 시점에 ``AttributeError``로 죽는다)."""
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
                print(f"[flood-runner] {block['id']}: connected YOLO {stype} source '{target}'"
                      f" (model={model_key or 'default'})")
                return src, stype
            except Exception as e:  # noqa: BLE001
                if restream_swap:
                    print(f"[flood-runner] {block['id']}: 재배포 서버 연결 실패, "
                          f"원본으로 전환하지 않습니다(정책) — {str(e)[:90]}")
                    return SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None), \
                        "restream_unavailable"
                print(f"[flood-runner] {block['id']}: {stype} '{target}' connection "
                      f"failed -> synthetic fallback ({str(e)[:90]})")
        return SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None), "synthetic"

    RESTREAM_RETRY_SEC = 300.0

    def _retry_restream_if_due(self, c) -> None:
        if c.get("kind") != "restream_unavailable":
            return
        now = time.time()
        if now < c.get("next_restream_retry_t", 0.0):
            return
        c["next_restream_retry_t"] = now + self.RESTREAM_RETRY_SEC
        source, kind = self._build_source(c["block"], c["rainfall"])
        if kind == "restream_unavailable":
            return
        bid = c["block"].get("id", "?")
        print(f"[flood-runner] {bid}: 재배포 연결 회복 확인 — 실제 판정을 재개합니다.")
        c["frames"] = source.frames()
        c["wh"] = source.frame_wh
        c["kind"] = kind
        c["water_model"] = (self._load_water_model()
                            if kind in ("hls", "rtsp", "video") else None)

    def _next_frame(self, c):
        try:
            return next(c["frames"])
        except StopIteration:
            src = SyntheticDetectionSource(c["rainfall"], fps=self.fps, duration_sec=None)
            c["frames"] = src.frames()
            c["wh"] = src.frame_wh
            c["kind"] = "synthetic(fallback)"
            c["water_model"] = None
            return next(c["frames"])

    # --- 물 모델(가중치 공유) ---------------------------------------------
    # runner.py::PipelineRunner의 같은 이름 메서드와 완전히 같은 이유·같은
    # 구현이다 — 지점이 몇 개든 모델은 하나만 만든다.

    def _load_water_model(self):
        if self._water_model_cached:
            return self._water_model_shared
        model = self._build_water_model()
        self._water_model_shared = model
        self._water_model_cached = True
        return model

    def _detect_water_backend(self, path) -> str:
        try:
            import torch
            ck = torch.load(str(path), map_location="cpu", weights_only=False)
        except Exception:  # noqa: BLE001
            return "ultralytics"
        if isinstance(ck, dict) and isinstance(ck.get("model"), dict) and "arch" in ck:
            return "torchvision"
        return "ultralytics"

    def _load_tv(self, path):
        from ..flood.water_segmentation_tv import load_tv_water_model
        model = load_tv_water_model(path, self._water_device)
        print(f"[flood-runner] water backend=torchvision (BSD) "
              f"model={path.name} conf={self._water_tv_conf}")
        return model

    def _load_ul(self, path):
        from ..core import inference
        model = inference.load(path, task="segment")
        print(f"[flood-runner] water backend=ultralytics (AGPL-3.0) model={path.name}")
        return model

    def _build_water_model(self):
        want = self._water_backend
        path = (self._water_tv_model_path if want == "torchvision"
                else self._water_model_path)
        order = ([want] if want in ("torchvision", "ultralytics")
                 else [self._detect_water_backend(path)])
        other = "ultralytics" if order[0] == "torchvision" else "torchvision"
        if want not in ("torchvision", "ultralytics"):
            order.append(other)
        last_err = ""
        for backend in order:
            try:
                model = (self._load_tv(path) if backend == "torchvision"
                         else self._load_ul(path))
                self._water_backend_effective = backend
                self._water_model_used = path
                self._water_load_error = ""
                return model
            except Exception as e:  # noqa: BLE001
                last_err = f"{backend}: {str(e)[:150]}"
                print(f"[flood-runner] water model load failed ({backend}, {path}): "
                      f"{str(e)[:120]}")
        self._water_backend_effective = ""
        self._water_model_used = path
        self._water_load_error = last_err
        return None

    def water_backend_status(self) -> dict:
        eff = self._water_backend_effective
        with self._ctx_lock:
            _ctx_snapshot = list(self._ctx.values())
        loaded = sum(1 for c in _ctx_snapshot if c.get("water_model") is not None)
        used = self._water_model_used or self._water_model_path
        err = self._water_load_error
        out = {
            "backend": self._water_backend,
            "effective_backend": eff or "(적재 실패)",
            "license": ({"torchvision": "BSD (torchvision)",
                         "ultralytics": "AGPL-3.0 (Ultralytics)"}.get(eff)
                        or "(확인 불가 — 모델을 열지 못했습니다)"),
            "model_path": str(used),
            "conf": (self._water_tv_conf if eff == "torchvision"
                     else self.water_cfg.get("water_conf", 0.10)),
            "models_loaded": loaded, "blocks": len(_ctx_snapshot),
            "ok": bool(eff) and loaded > 0,
        }
        if err:
            out["error"] = err
        if not out["ok"]:
            out["warning"] = ("침수 세그멘테이션이 돌고 있지 않습니다 — "
                              "탐지 0건이 「침수 없음」을 뜻하지 않습니다.")
        return out

    def flood_corrupted_skips_status(self) -> dict:
        with self._ctx_lock:
            _ctx_snapshot = list(self._ctx.values())
        per_camera = {c["block"]["id"]: c.get("corrupted_skips", 0)
                     for c in _ctx_snapshot if c.get("corrupted_skips", 0) > 0}
        return {"total": sum(per_camera.values()), "by_camera": per_camera}

    # --- 동적 재구성 -------------------------------------------------------

    def current_blocks(self) -> list[dict]:
        with self._ctx_lock:
            return [c["block"] for c in self._ctx.values()]

    def block_ids(self) -> set[str]:
        with self._ctx_lock:
            return set(self._ctx.keys())

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
            print(f"[flood-runner] 신규 지점 구성 실패 id={bid} — 건너뜁니다.")
            traceback.print_exc()
            return
        with self._ctx_lock:
            self._ctx[bid] = c
        self._start_worker(bid, c)
        print(f"[flood-runner] 신규 지점 상시 탐지 시작 id={bid}")

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
                print(f"[flood-runner] 경고 — 지점 {bid} 워커가 5초 안에 끝나지 "
                      "않았습니다(다음 틱에 스스로 빠져나옵니다).")
        try:
            self.store.remove(bid)
        except Exception:  # noqa: BLE001
            pass
        print(f"[flood-runner] 지점 상시 탐지 중단 id={bid}")

    def _desired_blocks(self) -> dict[str, dict]:
        from ..core import cameras as _cams
        from ..core.db import get_session
        from ..core.roles import Domain as _Dom

        db = get_session()
        try:
            return {b["id"]: b for b in _cams.continuous_blocks(db, _Dom.FLOOD.value)}
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
            print(f"[flood-runner] 상시 목록 재조정 — 추가 {len(added)}개 / "
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
                print("[flood-runner] 상시 목록 재조정 실패 — 다음 신호에 다시 봅니다.")
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

    def _update_flood(self, c, t: float, frame, vehicles, persons) -> None:
        if c["water_model"] is None or frame is None:
            return
        every = float(self.water_cfg.get("process_every_seconds", 1.0))
        if t - c["last_water_t"] < every:
            return
        c["last_water_t"] = t
        if is_likely_corrupted_frame(frame):
            c["corrupted_skips"] = c.get("corrupted_skips", 0) + 1
            if c["corrupted_skips"] in (1, 10) or c["corrupted_skips"] % 50 == 0:
                print(f"[flood-runner] treated as corrupted frame, skipping "
                      f"(total {c['corrupted_skips']})")
            return
        c["frame_no"] += 1
        try:
            if self._water_backend_effective == "torchvision":
                from ..flood.water_segmentation_tv import segment_water_tv
                water: WaterResult = segment_without_letterbox(
                    lambda f: segment_water_tv(
                        c["water_model"], f, conf=self._water_tv_conf),
                    frame)
            else:
                water = segment_without_letterbox(
                    lambda f: segment_water(
                        c["water_model"], f,
                        conf=self.water_cfg.get("water_conf", 0.10),
                        iou=self.water_cfg.get("iou", 0.50),
                        imgsz=self.water_cfg.get("imgsz", 640),
                        device=self._water_device,
                    ),
                    frame)
            box = mask_bbox(water.mask)
            fh, fw = water.mask.shape[:2]
            boxes = ([{"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3],
                      "label": "침수 영역"}] if box else [])
            _evidence.push_boxes(c["block"]["id"], boxes, (fw, fh))

            flood_m = c["flood_engine"].update(water, vehicles, persons, c["frame_no"], t)
            risk = c["risk_engine"].score(flood_m)
            flood_m.risk_score, flood_m.risk_grade = risk.risk_score, risk.risk_grade
            pred = c["risk_predictor"].update(flood_m)
            write_back(flood_m, risk, pred)
            _notify_flood_risk(self._notifier, flood_m, risk, c["block"]["name"],
                               min_grade=ug_settings.flood_notify_min_grade())
            # ⚠️ 2026-08-31(Phase 4) — 단순화: ByteTrack이 없어 ".stalled"를
            # 못 쓴다. "이 서비스가 감지한 차량 박스가 물 마스크와 겹치는가"
            # 만으로 판정한다(정지 여부는 안 본다) — 계획서 §Phase 4
            # "stopped_vehicles_near_water" 항목 참고.
            stopped_near_water = sum(
                1 for v in vehicles
                if point_on_mask(bottom_center(v.bbox), water.mask, pad=8)
            )
            c["last_flood"] = (flood_m, risk, pred, stopped_near_water)
        except Exception as e:  # noqa: BLE001
            print(f"[flood-runner] {str(e)[:120]}")

    def _block_loop(self, bid, c) -> None:
        own_stop = c.get("stop_ev")
        while not self._stop_ev.is_set() and not (own_stop and own_stop.is_set()):
            try:
                rev = config_rev.revision()
                if rev != c.get("roi_rev"):
                    new_roi = load_roi_for_camera(
                        bid, "flood",
                        fallback_path=PROJECT_ROOT / "configs" / "roi" / f"{bid}.json")
                    c["roi"] = new_roi
                    c["flood_engine"].set_roi(new_roi)
                    c["roi_rev"] = rev
                    print(f"[flood-runner:{bid}] ROI 다시 읽음 (rev={rev})")
            except Exception:  # noqa: BLE001
                print(f"[flood-runner:{bid}] ROI 재조회 실패 — 기존 ROI로 계속합니다.")
                traceback.print_exc()
            try:
                self._retry_restream_if_due(c)
                t, dets, frame = self._next_frame(c)
                restream_down = (c.get("kind") == "restream_unavailable")
                if frame is not None and not restream_down:
                    _evidence.push(bid, frame)
                vehicles, persons = _split_detections(dets)
                weather = c["rainfall"].at(t)
                river = c["river"].at(t)
                if not restream_down:
                    self._update_flood(c, t, frame, vehicles, persons)
                img = None
                if t - c["last_snap_t"] >= 1.0 and not restream_down:
                    img = (frame_to_image(frame, dets) if frame is not None
                           else render_scene(dets, weather, c["wh"]))
                    c["last_b64"] = to_jpeg_b64(img)
                    c["last_snap_t"] = t
                raining = weather.intensity != WeatherIntensity.none
                river_risk = c["river_agent"].infer(
                    river, raining=raining, context={"block": c["block"]["name"]})
                river_decision = c["river_decider"].decide(river_risk, t)
                if river_decision.alert:
                    _notify_river(self._notifier, river_decision, c["block"]["name"])
                flood_m = c.get("last_flood")
                snap = _flood_snapshot(
                    c["block"], t, weather, river, river_decision, c["last_b64"],
                    c["kind"], flood_m, c.get("corrupted_skips", 0),
                    _video_quality.grade_for(bid))
                self.store.update(bid, snap)
                # ⚠️ 2026-08-31(Phase 4) — 홈 화면(platform-shell)이 이
                # 프로세스의 store를 더는 직접 못 읽으므로 DB에도 남긴다.
                # DB 왕복이 있으므로 1초로 눌러 쓴다 — 이 루프는 최대 5fps로
                # 도는데 그 속도로 매번 DB에 쓰면 카메라 수만큼 초당 부하가
                # 커진다(image_snapshot 갱신과 같은 1초 주기를 따른다).
                if t - c["last_live_state_t"] >= 1.0:
                    c["last_live_state_t"] = t
                    # ★ event_sync.py가 이벤트·이력에 쓰는 것과 **같은
                    #   등급 매핑**(RiskEngine 5단계 → 행안부 4단계)을 써야
                    #   한다 — 홈 화면·이벤트 목록·이력이 서로 다른 등급을
                    #   말하면 안 된다. `flood_m[1].grade_label`(RiskEngine
                    #   자체 5단계 라벨)을 그대로 쓰면 안 됨 — 처음에 그렇게
                    #   짰다가 이 주석을 남기며 바로잡았다.
                    from .event_sync import _FLOOD_GRADE_LEVEL
                    level = ""
                    if flood_m is not None:
                        level = _FLOOD_GRADE_LEVEL.get(flood_m[1].risk_grade, "")
                    live_state.upsert(bid, "flood", level=level,
                                      is_available=bool(snap.get("water_available")))
            except Exception as e:  # noqa: BLE001
                try:
                    if not c.get("err_traced"):
                        c["err_traced"] = True
                        print(f"[flood-runner:{bid}] 첫 오류 — 아래 트레이스백은 "
                              f"이 지점에서 한 번만 남깁니다.")
                        traceback.print_exc()
                    print(f"[flood-runner:{bid}] {str(e)[:120]}")
                except Exception:  # noqa: BLE001
                    print(f"[flood-runner:{bid}] error (log message encoding failed)")
            self._stop_ev.wait(self.dt)
