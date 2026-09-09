"""Background pipeline runner — updates per-block 5-stage inference + flood
risk (measurement/prediction) at a live pace.

Ported from flood3's ``service/runner.py``, with import paths updated for the
merged package layout (docs/integration_plan.md Phase 7) and two behavioral
changes carried over from earlier phases:
  - the duplicated ``_load_yaml`` helper is now ``common.config``'s shared
    loader (Phase 1);
  - notification goes through the promoted ``common.notifier.AlertNotifier``
    instead of the retired per-block ``DryRunNotifier`` (Phase 4) — one
    shared notifier instance is used for all blocks (env-configured
    recipients aren't per-block in the SOLAPI settings model), with the
    ``run_poc.py``-style message-building helper reused via
    :func:`_notify_decision`.

**1 block = 1 camera (source) + per-block rainfall + adjacent river**.
Declared per block in blocks.json:
  source  : {"type":"synthetic"|"hls"|"rtsp"|"video", ...}
  rainfall: {"type":"sine"(default)|"mock"|"kma"}
  river   : {"type":"none"(default)|"mock"|"hrfco", advisory_m/warning_m/danger_m, ...}
A failed video/stream/external-API only degrades that one signal (the
dashboard keeps running). VLM is only called at snapshot time (~1s) to save
cost (falls back to rules if no key).

★ Flood risk (① water area ② expansion ③ RiskEngine ④ RiskPredictor):
  for real video sources (hls/rtsp/video) water segmentation
  (``models/best.pt``) runs on the received frame and is scored via
  ``flood.flood_metrics_engine``/``flood.risk_engine``. The existing vehicle
  detection/ByteTrack/rainfall pipeline is unchanged and its output
  (VehicleObject, TrafficMetrics, person points) is reused for traffic/person
  signals (no duplicate detection). Runs at a lower cadence (default 1s) than
  vehicle tracking (5fps) since water changes slowly.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from ..common.config import load_config_with_nested_merge, PROJECT_ROOT
from ..core import config_rev
from ..core import evidence as _evidence
from ..core import settings as ug_settings
from ..core import video_quality as _video_quality
from ..common.models_loader import resolve_device
from ..common.notifier import AlertNotifier, DuplicateNotificationError, NotificationConfigurationError
from ..common.roi import load_roi_config, load_roi_for_camera
from ..flood.flood_metrics_engine import FloodMetricsEngine
from ..flood.metrics_core import bottom_center, point_on_mask
from ..flood.risk_engine import RiskEngine, RiskPredictor, write_back
from ..flood.river_ontology import FLOOD_RIVER_CATALOG
from ..flood.river_provider import build_river
from ..flood.river_risk import FloodRiverAgent
from ..flood.water_segmentation import WaterResult, is_likely_corrupted_frame, segment_water
from ..models import Decision, RiskLevel, WeatherIntensity
from ..traffic_weather.agents.risk_decision import RiskDecisionAgent
from ..traffic_weather.agents.semantic_agent import (
    TRAFFIC_SEMANTIC_DEFAULTS,
    SemanticAgent,
)
from ..traffic_weather.agents.vlm_situation import VlmSituationAgent
from ..traffic_weather.knowledge.ontology import TRAFFIC_RISK_CATALOG
from ..traffic_weather.perception.detection_source import SyntheticDetectionSource
from ..traffic_weather.perception.perception import Perception
from ..traffic_weather.perception.rainfall_provider import SineRainfallProvider, build_rainfall
from ..traffic_weather.perception.render import frame_to_image, render_scene, to_jpeg_b64

_RISK_DEFAULTS = {
    # ★ 2026-08-21 flood/traffic 도메인 분리: "traffic" 가중치(0.06) 제거.
    #   나머지 6개 요소의 상대 비율은 그대로이며, RiskEngine이 자동 재정규화한다.
    "weights": {"area": 0.28, "low_point": 0.24, "lane": 0.10, "expansion": 0.10,
                "tire": 0.12, "person": 0.10},
    "expansion_ref": 0.05, "person_danger_floor": 85.0, "shutdown_rising_floor": 90.0,
    "grade_bins": [20, 40, 60, 80],
    "ratio_watch": 0.02, "ratio_caution": 0.08, "ratio_danger": 0.20, "ratio_shutdown": 0.35,
    "horizons_sec": [5, 10], "predict_window_sec": 12.0, "predict_min_points": 3,
    "trend_surge": 0.03, "trend_rising": 0.005, "trend_falling": -0.005, "eta_max_sec": 600,
}
_WATER_DEFAULTS = {
    # water_backend: "ultralytics"(기존, AGPL-3.0) | "torchvision"(신규, BSD)
    #
    # torchvision 백엔드는 자체 구축 데이터셋으로 학습한 시맨틱 세그멘테이션
    # 모델을 쓴다(val F1 0.9392). 기존 Ultralytics 모델이 실제 침수 영상에서
    # 전혀 검출하지 못하던 문제와 AGPL 라이선스 문제를 함께 해결한다 —
    # docs/flood_water_dataset_workflow.md 3-B절, docs/yolo_license_alternatives.md 참고.
    #
    # 전환 방법: configs/model_config.yaml 등에서 water_backend를 바꾸거나
    # 환경변수 TOT_WATER_BACKEND로 재정의(아래 PipelineRunner 참고).
    #
    # ★ 2026-08-19 기본값을 "auto" 로 바꿨다.
    #
    #   백엔드(로더)와 모델 파일이 **따로** 정해지는 구조였다. 백엔드는 이
    #   설정에서, 파일은 S-61 화면에서 정해진다. 둘이 어긋나면 적재가 조용히
    #   실패하고 `None` 이 되어 **침수 세그멘테이션이 아예 안 돈다.**
    #
    #   실제로 그렇게 돼 있었다 — 백엔드는 ultralytics 인데 화면이 고른 파일은
    #   torchvision 체크포인트라 `models_loaded: 0`, 3개 지점 전부 미동작.
    #   그런데 /api/health 는 모델 경로와 "AGPL-3.0" 을 그대로 표시했다.
    #   **아무것도 안 도는데 도는 것처럼 보였다.**
    #
    #   "auto" 는 **모델 파일을 보고** 로더를 정한다. 사람이 두 곳을 맞춰
    #   두어야 하는 구조 자체를 없앤다. 명시적으로 적으면 그쪽을 따른다.
    "water_backend": "auto",
    "water_model_path": "models/best.pt", "water_conf": 0.10,
    # torchvision 백엔드 전용. conf는 물 클래스 확률 임계값(0~1)이라 의미가 달라
    # 별도 키로 둔다(Ultralytics의 0.10을 그대로 쓰면 과검출).
    "water_tv_model_path": "data/datasets/flood_water_own/runs/flood_lraspp_384_v2/best.pt",
    "water_tv_conf": 0.50,
    "iou": 0.50, "imgsz": 640, "device": "auto", "process_every_seconds": 1.0,
}


def _flood_model_rel(config_default: str) -> str:
    """침수 상시 탐지가 쓸 모델 경로. **화면에서 고른 것이 우선한다.**

    지금까지 이 값은 ``configs/water_config.yaml`` 만 봤다. 그래서 관리자가
    S-61 에서 모델을 골라 저장해도 **파이프라인은 예전 모델을 계속 물고**
    있었고, 화면은 「재기동하면 반영됩니다」라고 안내했지만 재기동해도
    바뀌지 않았다. 「바뀐 줄 알고 관제하는」 상태다.

    설정이 비어 있거나 그 파일이 없으면 설정 파일 값으로 되돌아간다 —
    **고를 수 없게 되는 것보다 예전 모델로 도는 편이 낫다.**
    """
    try:
        from ..core import model_ops
        chosen = (model_ops.selected_key("flood") or "").strip()
        if chosen and (PROJECT_ROOT / chosen).exists():
            if chosen != config_default:
                print(f"[runner] water model from settings: {chosen}")
            return chosen
        if chosen:
            print(f"[runner] 지정된 침수 모델을 쓸 수 없어 설정파일 값으로 갑니다: {chosen}")
    except Exception as e:  # noqa: BLE001
        # DB 가 없어도 파이프라인은 떠야 한다.
        print(f"[runner] 침수 모델 설정 조회 실패(설정파일 사용): {str(e)[:120]}")
    return config_default


def _notify_decision(notifier: AlertNotifier, decision, block_name: str) -> None:
    """Build a generic alert message from a Decision and send it (dry-run by
    default; see ``traffic_weather.run_poc._notify``, the same pattern)."""
    body = f"[{decision.level.value}] {block_name} — {decision.risk_name}: {decision.recommendation}"
    message = {
        "body": body, "severity_label": decision.level.value, "location": block_name,
        "context": decision.context, "recommendation": decision.recommendation,
        "video_time": f"{decision.t_sec:.1f}s", "risk_score": decision.score,
    }
    try:
        notifier.send(event_key=f"{block_name}:{decision.risk_code}", message=message,
                      channels=["sms"])
    except (DuplicateNotificationError, NotificationConfigurationError):
        pass  # cooldown or not configured -- same as the original silent gate


def _notify_flood_risk(notifier: AlertNotifier, flood_m, risk, block_name: str,
                       min_grade: int = 4) -> None:
    """★ 2026-08-21 신규 — 침수 위험도(RiskEngine)를 독립적으로 SOLAPI 알림
    발생시킨다(확정사항 ③, docs/202608210801 3-4절).

    예전에는 flood_risk_score/grade가 화면 표시용일 뿐이었고, SOLAPI 알림은
    오직 교통·기상 판정(``_notify_decision``)에서만 나갔다 — 실제로 물이
    차오르는데 교통 정체가 아직 심하지 않으면 알림이 안 나가는 공백이 있었다.

    event_key에 등급을 포함시켜(``flood_risk:grade{N}``) `_notify_decision`
    (``{block}:{risk_code}``, risk_code는 TWR_*/FR_*)과 절대 겹치지 않는다 —
    혼합 상황에서 교통·침수 알림이 각각 독립적으로 나갈 수 있다(확정사항 ②).

    ⚠️ min_grade=4(「높음」) 는 부산 실환경에서 검증되지 않았다 — 확정 전
    재난 담당부서 협의가 필요하다(configs/risk_config.yaml의 기존 경고와
    같은 성격).
    """
    if risk.risk_grade < min_grade:
        return
    body = (f"[{risk.grade_label}] {block_name} — 침수위험도 "
            f"{risk.risk_score:.0f}점 ({risk.top_reason})")
    message = {
        "body": body, "severity_label": risk.grade_label, "location": block_name,
        "context": risk.top_reason, "recommendation": "현장 확인 및 통제 검토 권고",
        "video_time": f"{flood_m.timestamp_sec:.1f}s", "risk_score": risk.risk_score,
    }
    try:
        notifier.send(event_key=f"{block_name}:flood_risk:grade{risk.risk_grade}",
                      message=message, channels=["sms"])
    except (DuplicateNotificationError, NotificationConfigurationError):
        pass


def _no_traffic_decision(t_sec: float) -> Decision:
    """교통위험 미지정 카메라(``traffic_enabled=False``)용 중립 판정.

    ★ 2026-08-28 — 침수만 상시로 켠 카메라는 VLM 호출·semantic·decider를
    아예 부르지 않는다(교통 판정을 하지 않았으니 지어내지 않는다). 그런데
    `_snapshot()`은 여전히 `decision` 객체 하나를 기대한다 — 이 자리를
    비우면 스냅샷 딕셔너리 구성 자체가 죽어 침수 결과까지 함께 날아간다.
    그래서 「관심(가장 낮은 등급)·심각도 0·경보 없음」인 중립값을 채운다 —
    실제 판정이 아니라 자리 채움이라는 것은 `risk_code=""`(TWR_* 네임스페이스
    밖)와 `_sync_traffic()`의 ``traffic_enabled`` 게이트로 구분한다.
    """
    return Decision(t_sec=t_sec, risk_code="", risk_name="", level=RiskLevel.interest,
                     severity=0, score=0.0, recommendation="", drivers=[],
                     context="", alert=False)


def _restream_whep_url(camera_id: str) -> str | None:
    """관제요원 브라우저가 WHEP으로 재생할 주소. 재배포가 꺼져 있거나
    MediaMTX가 응답하지 않으면 ``None`` — `app.js::openLive()`가 그 경우
    기존 hls.js 경로로 자연히 떨어지게 한다.

    ⚠️ 매 스냅샷(초당 약 1회)마다 불리므로, `mediamtx_healthy()`의 TTL
    캐시(10초)가 실제 HTTP 호출 빈도를 크게 줄여 준다. **설정 조회는
    캐시만 보고 DB 세션을 새로 열지 않는다** — 이 함수는 `_block_loop`
    (lifespan이 끝난 뒤에만 도는 상시 루프) 안에서만 불리므로, `core/
    cameras.py::to_block_dict()`가 겪은 "lifespan 시작 전 캐시가 비어
    있는" 함정(모듈 최상단의 `BLOCKS = _load_blocks()`)이 여기는 해당되지
    않는다 — 이 함수가 처음 불리는 시점엔 이미 `load_all()`이 끝나 있다.

    ⚠️ 2026-08-29 — "재배포 가능한가" 판단(enabled→제외목록→헬스체크)은
    이제 `core/restream.py::resolved_whep_url()` 하나로 합쳤다(이전에는
    `service/main.py::_restream_whep_url_for()`와 이 함수가 각각
    구현하고 있다가 같은 버그(제외 목록 미확인)가 두 곳에서 따로
    발견·수정된 전례가 있다). 여기서는 db 없이(캐시만) 위임해 기존
    동작을 그대로 보존한다 — `request_host`는 이 함수 호출부
    (백그라운드 스레드, Request 없음)가 원리적으로 몰라 넘기지 않는다.
    실제 원격 접속용 호스트로 다시 계산하는 지점은 `main.py::
    _rewrite_whep_urls()`(서빙 시점, R-01)를 참고.
    """
    from ..core import restream as _restream
    return _restream.resolved_whep_url(camera_id)


def _snapshot(block, weather, metrics, situation, decision, river, snap_b64, vlm_src, kind,
              vehicles=None, flood=None, incidents=None, corrupted_skips=0,
              quality_grade=None) -> dict:
    objs = sorted(vehicles or [], key=lambda v: -v.speed_drop)[:6]
    # ★ 2026-08-26 — 예전에는 여기서 block["calib"]["mpp"]를 읽었는데, 그
    #   키를 채우는 코드가 어디에도 없어 항상 기본값 0.06(픽셀속도×0.216
    #   고정계수)으로 떨어졌다 — 화면·이벤트·보고서에 나가는 "평균속도
    #   N km/h"가 전부 근거 없는 값이었다(docs/202608260842/ 계획 §1).
    #   이제 metrics.mean_speed_kmh/v.speed_kmh(traffic_tracker.py가
    #   core.calibration.traffic_speed_kmh로 실측)를 그대로 쓴다.
    #   지면 보정이 없으면 None — "미보정"이라고 정직하게 비워 둔다.
    _src_cfg = block.get("source") or {}
    # ★ 2026-08-28 — CCTV 재배포 허브. `to_block_dict()`가 재배포를 켰을 때
    # `source.url`을 내부용 RTSP 주소로 치환하고 원본은 `origin_url`에
    # 남겨 둔다(core/cameras.py 참고). 브라우저(hls.js)는 RTSP를 재생할 수
    # 없으므로, 화면에 내려주는 `stream_url`은 **항상 원본 HLS 주소**여야
    # 한다 — `origin_url`이 있으면 그것을, 없으면(재배포 꺼짐) 기존처럼
    # `url`을 그대로 쓴다.
    _browser_stream_url = _src_cfg.get("origin_url") or _src_cfg.get("url")
    snap = {
        "mean_speed_kmh": metrics.mean_speed_kmh,
        "stream_url": _browser_stream_url,
        "stream_type": _src_cfg.get("type"),
        # 관제요원 브라우저가 WHEP(WebRTC)으로 재생할 주소. 재배포가 꺼져
        # 있거나 응답이 없으면(관측 없음 상태) None — 화면이 hls.js로
        # 자연히 떨어지게 한다(app.js::openLive 참고).
        "whep_url": (_restream_whep_url(block["id"])
                    if kind != "restream_unavailable" else None),
        "block_id": block["id"], "name": block["name"],
        "coordinates": block["coordinates"], "source_kind": kind,
        # ★ 2026-08-28 — CCTV 재배포 허브. kind="restream_unavailable"이면
        # 재배포 서버 장애로 판정을 아예 안 돌린 것이다(폴백 없음 정책,
        # 확정) — 이 필드로 화면이 "관측 없음"을 정직하게 표시한다
        # (crowd_history.summary()의 observed=False 관례와 동일).
        "observed": kind != "restream_unavailable",
        # ★ 2026-08-28 — `_sync_traffic()`이 이 값으로 「교통위험을 실제로
        # 판정했는가」를 가려 이력·이벤트 생성 여부를 정한다(water_available이
        # 침수 쪽에서 하는 것과 같은 역할). 카메라의 실제 지정 상태이지
        # 이번 틱에 판정을 돌렸는지가 아니다 — 미지정 카메라는 애초에
        # `decision`이 중립값(`_no_traffic_decision`)이라 매번 False다.
        "traffic_enabled": bool(block.get("traffic_enabled", True)),
        "t_sec": metrics.t_sec, "rain_mm_h": weather.rain_mm_h,
        "intensity": weather.intensity.value, "rain_source": weather.source,
        "rain_trend": weather.trend, "rain_delta": weather.delta_mm_h,
        "mean_speed": metrics.mean_speed, "speed_drop": metrics.speed_drop,
        "state": metrics.state.value, "queue_len": metrics.queue_len,
        "stalled": metrics.stalled, "n_vehicles": metrics.n_vehicles,
        "objects": [{"id": v.track_id, "speed": v.speed,
                     "kmh": v.speed_kmh, "drop": v.speed_drop,
                     "stalled": v.stalled} for v in objs],
        "river_level": round(river.level_m, 2) if river else None,
        "river_status": river.status.value if river else None,
        "river_source": river.source if river else None,
        "risk_code": decision.risk_code, "risk_name": decision.risk_name,
        "level": decision.level.value, "severity": decision.severity,
        "score": decision.score, "recommendation": decision.recommendation,
        "situation_ko": situation.get("situation_ko", ""),
        "vlm_source": vlm_src, "drivers": decision.drivers, "alert": decision.alert,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "snapshot": snap_b64,
        # 돌발상황(보행자·역주행·사고 의심) — hold 창 안의 활성 사건 목록
        # (2026-08-26, incident_events.TrafficIncident.to_dict() 형태).
        "incidents": incidents or [],
        # ⚠️ 2026-08-30 — 상시 화질 감시 1단계. `is_likely_corrupted_frame()`
        # (flood/water_segmentation.py)이 손상으로 보고 건너뛴 누적
        # 횟수는 이전까지 c["corrupted_skips"]에만 쌓이고 `/api/risk`·
        # `/api/health` 어디에도 안 나갔다 — 침수 쪽조차 "지금 얼마나
        # 자주 건너뛰고 있는지" 볼 방법이 없었다. 새 신호를 만드는 게
        # 아니라 있던 신호를 그대로 실어 보낸다.
        "flood_corrupted_skips": corrupted_skips,
        # ⚠️ 2026-08-30 — 상시 화질 감시 4단계. warn/crit이면 화면이
        # 실시간 영상을 열기 전에도 배지로 미리 알려준다(road/crowd
        # 카드와 같은 방식, app.js).
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
            # ★ 2026-08-21: FloodMetrics 자체 필드가 아니라 orchestrator가
            #   조합한 값(위 _update_flood 참고) — flood/traffic 분리 이후에도
            #   화면 표시 항목은 그대로 유지한다.
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
        # 침수심 대응표를 보정한 지점이면 **추정 침수심(cm)** 을 함께 낸다.
        # 정부 기준(지하차도 통제 5cm · NWS/FEMA 15·30cm)은 전부 침수심
        # 기준이라, 면적 비율(%)로는 그 기준과 연결할 수 없다
        # (docs/202608151713/threshold_rationale.md 2절).
        depth, level, saturated = _depth_of(block["id"], m.water_area_ratio)
        snap.update({
            "flood_depth_cm": depth,          # 보정 전에는 None
            "flood_depth_level": level,       # 보정 전에는 "미보정"
            # 관측 범위를 넘은 값인가. 화면이 「이 이상」으로 표시하도록 —
            # 외삽하지 않으므로 상한에 묶인 값을 그대로 믿으면 안 된다.
            "flood_depth_saturated": saturated,
        })
    else:
        snap["water_available"] = False
    return snap


def _depth_of(camera_id: str, water_ratio: float):
    """(추정 침수심cm, 단계, 관측범위초과). 보정 전에는 (None, "미보정", False).

    DB를 못 읽어도 탐지가 멈추면 안 되므로 실패는 삼키고 미보정으로 내려간다.
    """
    try:
        from ..core import calibration
        from ..core import cameras as _cams
        from ..core.db import get_session
        db = get_session()
        try:
            cam = _cams.get(db, camera_id)
            if cam is None:
                return None, "미보정", False
            cal = calibration.of(cam, "flood")
            depth = calibration.flood_depth_cm(cal, water_ratio)
            saturated = bool(cal.depth and cal.depth.saturated(water_ratio))
            return (round(depth, 1) if depth is not None else None,
                    calibration.flood_level(depth, db=db), saturated)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return None, "미보정", False


class PipelineRunner(threading.Thread):
    """침수 상시 탐지 — 지점 하나당 워커 스레드 하나.

    ★ 2026-08-22 **동적 재구성**(``dynamic=True``)
        예전에는 기동 시점의 목록을 프로세스가 죽을 때까지 그대로 물고
        있었다. S-80 에서 카메라를 새로 등록하고 「침수=상시」로 지정해도
        **서비스를 다시 띄우기 전에는 아무 일도 일어나지 않았다** — 화면에는
        「상시」로 뜨는데 실제로는 아무도 그 지점을 보지 않는 상태였다
        (노면 워처는 이미 ``config_rev`` 를 구독해 이 문제가 없었다).

        이제 ``config_rev`` 변경을 구독하는 수퍼바이저 스레드가 목록을 다시
        읽어, 추가된 지점은 워커를 띄우고(``_spawn_block``) 빠진 지점은
        정지시킨다(``_retire_block``).

        ⚠️ **이미 상시 중인 지점의 부가 설정**(rain/river/source 등) 변경은
        이번 범위에서 스레드를 갈아 끼우지 **않는다** — 추적 상태
        (``ExpansionRateTracker``·``RiskPredictor``)가 통째로 사라지기
        때문이다. ROI 변경은 ``_block_loop`` 이 스레드를 유지한 채 갈아
        끼운다(위 ``load_roi_for_camera`` 참고).

        ``dynamic=False`` 면 수퍼바이저를 아예 띄우지 않아 **예전과 100%
        같게 동작한다** — 시험 환경(``TOT_BLOCKS_PATH`` 고정)이 이 경로를 쓴다.
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
        # _ctx 자체(삽입·삭제·스냅샷)만 보호한다 — 각 워커가 자기 c 안을
        # 읽고 쓰는 것은 그 스레드 전용이라 락이 필요 없다(기존 전제 유지).
        self._ctx_lock = threading.RLock()
        self._workers: list = []
        self._notifier = AlertNotifier()

        self.risk_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "risk_config.yaml", _RISK_DEFAULTS)
        # 2026-08-24 — 교통위험 판정 임계값(왼쪽 메뉴 "교통위험 › 위험도
        # 임계값" 화면, S-82와 대칭). 침수와 같은 이유로 재시작해야 반영된다
        # (routes_config.py 의 공용 YAML 저장 핸들러가 이미 그렇게 안내한다).
        self.traffic_risk_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "traffic_risk_config.yaml",
            TRAFFIC_SEMANTIC_DEFAULTS)
        self.water_cfg = load_config_with_nested_merge(
            PROJECT_ROOT / "configs" / "water_config.yaml", _WATER_DEFAULTS)
        self._water_device = resolve_device(self.water_cfg.get("device", "auto"))
        self._water_model_path = PROJECT_ROOT / _flood_model_rel(
            self.water_cfg["water_model_path"])
        # 환경변수로 재정의 가능 -- 설정파일을 고치지 않고 백엔드를 바꿔 비교할 수 있게
        # 물 모델은 지점이 몇 개든 하나만 만든다(_load_water_model 참고).
        # 실패도 기억한다 — 안 그러면 블록마다 다시 시도한다.
        self._water_model_shared = None
        self._water_model_cached = False
        self._water_backend = (os.environ.get("TOT_WATER_BACKEND")
                               or self.water_cfg.get("water_backend", "auto")).strip()
        # 적재 결과를 기억한다 — 상태 보고가 **요청값이 아니라 실제값**을
        # 말하게 하려는 것이다(water_backend_status 참고).
        self._water_backend_effective = ""
        self._water_model_used = None
        self._water_load_error = ""
        self._water_tv_model_path = PROJECT_ROOT / self.water_cfg["water_tv_model_path"]
        self._water_tv_conf = float(self.water_cfg.get("water_tv_conf", 0.50))

        for b in blocks:
            self._ctx[b["id"]] = self._build_block_ctx(b)

    def _build_block_ctx(self, b: dict) -> dict:
        """지점 하나의 실행 컨텍스트(소스·판정기·엔진 묶음)를 만든다.

        ``__init__`` 안에 인라인으로 있던 것을 뽑아냈다 — 동적 재구성
        (``_spawn_block``)이 기동 후에도 같은 방식으로 지점을 만들 수 있어야
        하기 때문이다(2026-08-22). 만드는 내용 자체는 예전과 같다.
        """
        rp = b.get("rain", {})
        sine = SineRainfallProvider(peak_mm_h=rp.get("peak", 25.0),
                                    period_sec=rp.get("period", 48.0),
                                    phase_sec=rp.get("phase", 0.0))
        # 카메라별 명시 설정이 없으면 전역 기본값(S-95,
        # ug_settings.rainfall_backend)을 쓴다 — 39개소를 하나씩 고치지
        # 않고도 실측(KMA)/합성(sine)을 한 번에 바꿀 수 있게 하려는 것이다
        # (2026-08-27, 교통위험 실측 연동 요청).
        #
        # ⚠️ **캐시(``ug_settings.get(key)``, db 인자 없이)를 믿지 않는다.**
        #   이 함수는 ``main.py`` 모듈 최상단(``runner = PipelineRunner(...)``)
        #   에서 **lifespan 시작 전에** 불린다 — 그때는 설정 캐시가 아직
        #   비어 있어(``ug_settings.load_all(db)``가 lifespan 안에서만
        #   실행됨) 항상 DEFAULTS(sine)로 떨어진다(2026-08-27 실측
        #   확인 — 화면에서 kma로 저장·재기동해도 조용히 sine으로 남는
        #   결함이었다). 여기서 직접 세션을 열어 DB 값을 읽는다.
        # DB가 아직 준비되지 않은 시점(마이그레이션 전 최초 기동, 시험 환경의
        # 스키마 생성 전 import 등)에 이 조회 하나 때문에 지점 구성 전체가
        # 죽으면 안 된다 — 실패하면 안전한 기본값(sine)으로 눕힌다. 이미
        # `_load_water_model()` 등 옆 코드들이 쓰는 것과 같은 원칙이다.
        try:
            from ..core.db import get_session as _get_settings_session
            _sdb = _get_settings_session()
            try:
                _rain_backend = ug_settings.rainfall_backend(_sdb)
            finally:
                _sdb.close()
        except Exception as e:  # noqa: BLE001
            print(f"[rainfall] 전역 설정 조회 실패, sine으로 진행: {str(e)[:120]}")
            _rain_backend = "sine"
        rainfall = build_rainfall(b.get("rainfall"), b["coordinates"],
                                  fallback=sine, default_type=_rain_backend)
        river = build_river(b.get("river"))
        source, baseline_hint, kind = self._build_source(b, rainfall)
        # ⚠️ DB(camera_rois) 를 먼저 본다 — 웹 ROI 편집기(S-81)는 DB에만
        #   저장하는데, 예전에는 이 파이프라인이 파일만 읽어서 웹에서
        #   그린 ROI 가 실제 판정에 반영되지 않았다(2026-08-22 전수점검
        #   → common/roi.load_roi_for_camera() 신설로 해결,
        #   scripts/backfill_roi_files_to_db.py 로 기존 파일을 먼저
        #   옮겨 둔 뒤 배포). 파일은 DB에도 없을 때만 쓰는 폴백이다.
        roi = load_roi_for_camera(
            b["id"], "flood",
            fallback_path=PROJECT_ROOT / "configs" / "roi" / f"{b['id']}.json")
        ctx = {
            "block": b, "rainfall": rainfall, "river": river,
            "frames": source.frames(), "wh": source.frame_wh, "kind": kind,
            "perception": Perception(b, rainfall, fps=self.fps,
                                     baseline_hint=baseline_hint, use_bytetrack=True),
            "vlm": VlmSituationAgent(use_vlm=self.use_vlm, location=b["name"]),
            # ★ 2026-08-21 flood/traffic 도메인 분리: 교통(semantic/decider)과
            #   침수·하천(river_agent/river_decider)이 완전히 독립된 카탈로그로
            #   각자 판정한다(TRAFFIC_RISK_CATALOG의 TWR_* vs FLOOD_RIVER_CATALOG의
            #   FR_*, 코드 네임스페이스가 겹치지 않는다).
            "semantic": SemanticAgent(self.traffic_risk_cfg),
            "decider": RiskDecisionAgent(TRAFFIC_RISK_CATALOG),
            "river_agent": FloodRiverAgent(),
            "river_decider": RiskDecisionAgent(FLOOD_RIVER_CATALOG),
            "last_snap_t": -999.0, "last_b64": None,
            "water_model": self._load_water_model() if kind in ("hls", "rtsp", "video") else None,
            "roi": roi,
            # 이 값을 기록해 둔 시점의 리비전 — _block_loop 이 매 틱
            # config_rev.revision() 과 비교해, 바뀐 경우에만 ROI 를
            # 다시 읽는다(DB 조회를 매 프레임 하지 않기 위함).
            "roi_rev": config_rev.revision(),
            "flood_engine": FloodMetricsEngine(roi=roi),
            "risk_engine": RiskEngine(self.risk_cfg, self.risk_cfg),
            "risk_predictor": None,  # filled in below once risk_engine exists
            "last_water_t": -999.0, "last_flood": None,
            "frame_no": 0,
            # 이 지점만 따로 멈추기 위한 신호 — 동적 제외(_retire_block)에
            # 쓴다. 전체 정지(self._stop_ev)와 OR 로 함께 확인한다.
            "stop_ev": threading.Event(),
            "thread": None,   # _spawn_block/run() 이 채운다
        }
        ctx["risk_predictor"] = RiskPredictor(ctx["risk_engine"], self.risk_cfg)
        return ctx

    def water_backend_status(self) -> dict:
        """현재 물 세그멘테이션 백엔드 정보 (/api/health 노출용).

        어느 모델이 실제로 돌고 있는지는 운영 중 구분이 어렵다(맑은 날에는
        어느 백엔드든 검출 0으로 똑같이 보인다). 라이선스가 다른 두 백엔드를
        오가는 구성이라 실제 적재된 쪽을 확인할 수단이 필요해 노출한다.
        """
        # ★ **요청한 백엔드가 아니라 실제로 적재된 백엔드**를 말한다.
        #   예전에는 요청값을 그대로 보고해서, 적재가 실패해도 화면에는
        #   모델 경로와 라이선스가 떠 있었다. 아무것도 안 도는데 도는 것처럼
        #   보였고, 심지어 **라이선스 표기까지 틀렸다**(torchvision 파일을
        #   AGPL 로 표시). 라이선스는 납품 검토에 그대로 들어가는 값이다.
        eff = getattr(self, "_water_backend_effective", "")
        # 동적 재구성 중에 _ctx 가 바뀔 수 있어 스냅샷을 떠서 센다
        # ("dictionary changed size during iteration" 방지).
        with self._ctx_lock:
            _ctx_snapshot = list(self._ctx.values())
        # ⚠️ 2026-08-23 — 침수가 미사용인 블록(교통위험만 상시)도 스트림이
        # 있으면 물 세그멘테이션 모델(공유 가중치)을 들고 있지만, 실제로는
        # `_block_loop`가 `flood_enabled`를 보고 절대 호출하지 않는다. 여기서
        # 그 블록까지 세면 "침수 지점이 N개 돈다"는 숫자가 실제보다 부풀어
        # 화면이 실제와 다른 것을 말하게 된다 — 침수가 실제로 도는 블록만 센다.
        flood_ctx = [c for c in _ctx_snapshot if c["block"].get("flood_enabled")]
        loaded = sum(1 for c in flood_ctx if c.get("water_model") is not None)
        used = getattr(self, "_water_model_used", None) or self._water_model_path
        err = getattr(self, "_water_load_error", "")
        out = {
            "backend": self._water_backend,          # 설정에 적힌 것
            "effective_backend": eff or "(적재 실패)",  # 실제로 열린 것
            "license": ({"torchvision": "BSD (torchvision)",
                         "ultralytics": "AGPL-3.0 (Ultralytics)"}.get(eff)
                        or "(확인 불가 — 모델을 열지 못했습니다)"),
            "model_path": str(used),
            "conf": (self._water_tv_conf if eff == "torchvision"
                     else self.water_cfg.get("water_conf", 0.10)),
            "models_loaded": loaded,
            "blocks": len(flood_ctx),
            # ⚠️ 「돌고 있는가」를 한 값으로 답한다. 화면이 표를 훑어 스스로
            #    판단하게 두면, 판단이 빠진 화면에서 조용히 지나간다.
            "ok": bool(eff) and loaded > 0,
        }
        if err:
            out["error"] = err
        if not out["ok"]:
            out["warning"] = ("침수 세그멘테이션이 돌고 있지 않습니다 — "
                              "탐지 0건이 「침수 없음」을 뜻하지 않습니다.")
        return out

    def flood_corrupted_skips_status(self) -> dict:
        """`is_likely_corrupted_frame()`이 손상으로 보고 건너뛴 누적 횟수
        (/api/health 노출용, 2026-08-30 상시 화질 감시 1단계).

        지금까지 이 카운터(c["corrupted_skips"])는 각 블록의 메모리에만
        쌓이고 로그(그마저 1·10·이후 50번마다만)로만 보였다 — 침수
        판정이 최근 얼마나 자주 "손상 프레임"으로 건너뛰고 있는지 아무도
        상시로 볼 수 없었다. water_backend_status()와 같은 스냅샷
        패턴으로 카메라별 값과 합계를 낸다.
        """
        with self._ctx_lock:
            _ctx_snapshot = list(self._ctx.values())
        per_camera = {c["block"]["id"]: c.get("corrupted_skips", 0)
                     for c in _ctx_snapshot if c.get("corrupted_skips", 0) > 0}
        return {
            "total": sum(per_camera.values()),
            "by_camera": per_camera,
        }

    def _load_water_model(self):
        """물 세그멘테이션 모델. **지점이 몇 개든 하나만 만든다.**

        지금까지 이 함수는 **블록마다 호출**돼 같은 가중치를 지점 수만큼
        메모리에 올렸습니다. 3지점이면 3벌입니다.

        **모델은 무상태입니다** — 프레임을 받아 결과를 돌려줄 뿐 아무것도
        기억하지 않으므로 공유해도 결과가 달라지지 않습니다. 지점별 상태
        (ROI·추적·기준선)는 분석기 쪽에 있습니다.

        ⚠️ **분석기를 공유하는 것과 다릅니다.** 분석기는 추적 ID 와 배회
        타이머를 들고 있어 공유하면 지점 간에 뒤섞입니다. **여기서 공유하는
        것은 가중치뿐입니다.**

        런타임을 바꾸면 이 절약이 커집니다 — OpenVINO 는 로드+예열이
        **26.7초**라(2026-08-17 실측) 지점마다 만들면 기동이 그만큼 늘어납니다.

        torchvision 백엔드가 실패해도 서비스 전체가 죽지 않도록, 실패 시
        None 을 반환해 물 세그멘테이션만 비활성화한다(기존 동작 유지).
        **실패도 기억합니다** — 안 그러면 블록마다 다시 시도하며 같은 오류를
        지점 수만큼 찍습니다.
        """
        if self._water_model_cached:
            return self._water_model_shared
        model = self._build_water_model()
        self._water_model_shared = model
        self._water_model_cached = True
        return model

    def _detect_water_backend(self, path) -> str:
        """모델 파일을 보고 어느 로더로 열어야 하는지 정한다.

        ``scripts/train_flood_water_cpu.py`` 가 저장한 torchvision 체크포인트는
        ``{"arch": ..., "model": <state_dict>}`` 꼴이다. Ultralytics 체크포인트의
        ``model`` 은 state_dict 가 아니라 **모듈 객체**라 이것으로 갈린다.

        ⚠️ 판별에 실패하면 **ultralytics 로 본다.** 기존 동작이라 바꿀 이유가
        없고, 틀리면 아래에서 반대쪽으로 한 번 더 시도한다.
        """
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
        print(f"[runner] water backend=torchvision (BSD) "
              f"model={Path(path).name} conf={self._water_tv_conf}")
        return model

    def _load_ul(self, path):
        # 공통 로더 — task 를 못박고 장치를 CPU 로 고정하며 예열한다.
        # 화면에서 ONNX·OpenVINO 후보를 고를 수 있게 되면서 필요해졌다.
        from ..core import inference
        model = inference.load(path, task="segment")
        print(f"[runner] water backend=ultralytics (AGPL-3.0) model={Path(path).name}")
        return model

    def _build_water_model(self):
        """물 모델을 적재하고 **실제로 적재된 백엔드를 기록**한다.

        ★ 기록이 핵심이다. 예전에는 요청한 백엔드를 그대로 보고해서,
        적재가 실패해도 화면에는 그 백엔드와 라이선스가 떠 있었다.
        **아무것도 안 도는데 도는 것처럼 보였다.**
        """
        want = self._water_backend
        # 명시적으로 적었으면 그 파일을 쓴다. auto 면 화면(S-61)이 고른 것을 쓴다.
        path = (self._water_tv_model_path if want == "torchvision"
                else self._water_model_path)
        order = ([want] if want in ("torchvision", "ultralytics")
                 else [self._detect_water_backend(path)])
        # 판별이 틀릴 수 있으니 반대쪽도 한 번 시도한다 — 두 번째 시도까지
        # 실패해야 「못 연다」고 말할 자격이 생긴다.
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
                print(f"[runner] water model load failed ({backend}, {path}): "
                      f"{str(e)[:120]}")

        # ⚠️ 실패를 삼키지 않는다. 여기서 남긴 것이 /api/health 로 나간다.
        self._water_backend_effective = ""
        self._water_model_used = path
        self._water_load_error = last_err
        return None

    def _build_source(self, block, rainfall):
        """Block source config -> (source, baseline_hint, kind). Falls back to synthetic."""
        cfg = block.get("source") or {"type": "synthetic"}
        stype = cfg.get("type", "synthetic")
        if stype in ("video", "rtsp", "hls"):
            target = cfg.get("url") or cfg.get("path")
            # ★ 2026-08-28 — CCTV 재배포 허브(MediaMTX). `to_block_dict()`가
            # 재배포로 치환했을 때만 `origin_url`을 함께 남겨 둔다 — URL
            # 문자열 패턴을 추측하지 않고 이 플래그로만 "재배포 주소인가"를
            # 판단한다. 아래 except 절의 "폴백 없음" 분기가 이 값을 쓴다.
            restream_swap = bool(cfg.get("origin_url"))
            try:
                from ..traffic_weather.perception.detection_source import YoloDetectionSource
                # ⚠️ model= 을 안 넘기면 YoloDetectionSource 자체의 하드코딩된
                #   기본값("models/yolo11s.pt")이 항상 쓰인다 — AI 모델
                #   관리 화면(/models)에서 교통 모델을 골라 저장해도 실제
                #   차량 검출은 그 값을 전혀 안 읽던 결함이다(2026-08-22
                #   전수점검). model_ops.selected_key() 가 설정→레지스트리
                #   기본값 순으로 실제 경로를 돌려준다 — 아무 것도 못 찾으면
                #   빈 문자열이라, 그때만 라이브러리 기본값으로 물러난다.
                from ..core import model_ops
                model_key = model_ops.selected_key("traffic") or None
                kwargs = {"fps": self.fps, "loop": True}
                if model_key:
                    kwargs["model"] = model_key
                src = YoloDetectionSource(target, **kwargs)
                print(f"[runner] {block['id']}: connected YOLO {stype} source '{target}'"
                      f" (model={model_key or 'default'})")
                return src, None, stype
            except Exception as e:  # noqa: BLE001
                if restream_swap:
                    # ★ 폴백 없음 정책(확정, 2026-08-28) — 재배포 서버 연결이
                    # 실패해도 원본 CCTV로 되돌아가지 않는다. 원본은 동시접속
                    # 한계 때문에 재배포를 도입한 것이라, 조용히 원본 직결로
                    # 돌아가면 재배포를 켜기 전 문제가 그대로 재발한다. 대신
                    # 이 kind로 표시해 `_block_loop`가 판정을 건너뛰고
                    # "관측 없음"으로 정직하게 남기게 한다(synthetic 데이터가
                    # 실제 판정처럼 쓰이면 안 되므로) — ctx 구성이
                    # source.frames()를 즉시 호출해 `None`을 돌려줄 수 없어
                    # SyntheticDetectionSource는 기계적 필요로만 재사용한다.
                    print(f"[runner] {block['id']}: 재배포 서버 연결 실패, "
                          f"원본으로 전환하지 않습니다(정책) — {str(e)[:90]}")
                    src = SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None)
                    return src, src.base_speed, "restream_unavailable"
                print(f"[runner] {block['id']}: {stype} '{target}' connection failed -> synthetic "
                      f"fallback ({str(e)[:90]})")
        src = SyntheticDetectionSource(rainfall, fps=self.fps, duration_sec=None)
        return src, src.base_speed, "synthetic"

    # 재배포(MediaMTX) 재연결 재시도 간격(실제 시계 기준, 초). ``_build_source``
    # 는 지점 구성 시점에 딱 한 번만 불려 실패하면 그 결과가 프로세스
    # 수명 내내 고정된다 — 2026-08-29 실사용 중 발견: MediaMTX 프로세스가
    # 재부팅 후 자동으로 안 살아나는 결함(scripts/urbanguard-run.cmd 참고)
    # 때문에 재배포가 몇 시간이고 죽어 있었는데, 그동안 살아 있던 서비스
    # 프로세스는 재기동 전까지 계속 "관측 없음"에 머물렀다(재배포를
    # 나중에 고쳐도 소용없었다). 너무 잦은 재시도는 회복 안 된 상태에서
    # 접속 시도 로그만 쌓이므로 5분으로 제한한다.
    RESTREAM_RETRY_SEC = 300.0

    def _retry_restream_if_due(self, c) -> None:
        """kind가 restream_unavailable인 지점을 주기적으로 재연결 시도한다.

        성공하면(재배포 서버가 복구됐으면) 서비스 재기동 없이도 실제
        판정으로 스스로 돌아온다. 실패하면 아무것도 바꾸지 않고 다음
        주기를 기다린다 — 매 틱 재시도하지 않는다.
        """
        if c.get("kind") != "restream_unavailable":
            return
        now = time.time()
        if now < c.get("next_restream_retry_t", 0.0):
            return
        c["next_restream_retry_t"] = now + self.RESTREAM_RETRY_SEC
        source, baseline_hint, kind = self._build_source(c["block"], c["rainfall"])
        if kind == "restream_unavailable":
            return  # 여전히 안 됨 — 다음 주기에 다시 시도
        bid = c["block"].get("id", "?")
        print(f"[runner] {bid}: 재배포 연결 회복 확인 — 실제 판정을 재개합니다.")
        c["frames"] = source.frames()
        c["wh"] = source.frame_wh
        c["kind"] = kind
        c["perception"] = Perception(c["block"], c["rainfall"], fps=self.fps,
                                     baseline_hint=baseline_hint, use_bytetrack=True)
        c["water_model"] = (self._load_water_model()
                            if kind in ("hls", "rtsp", "video") else None)

    def _next_frame(self, c):
        try:
            return next(c["frames"])
        except StopIteration:  # video ended (empty/short clip) -> synthetic fallback
            src = SyntheticDetectionSource(c["rainfall"], fps=self.fps, duration_sec=None)
            c["frames"] = src.frames()
            c["wh"] = src.frame_wh
            c["kind"] = "synthetic(fallback)"
            c["perception"] = Perception(c["block"], c["rainfall"], fps=self.fps,
                                         baseline_hint=src.base_speed, use_bytetrack=True)
            c["water_model"] = None  # no real frames in synthetic fallback -> no water seg
            return next(c["frames"])

    # --- 동적 재구성 (2026-08-22) --------------------------------------
    def current_blocks(self) -> list[dict]:
        """지금 실제로 돌고 있는 지점의 블록 dict 목록(스냅샷)."""
        with self._ctx_lock:
            return [c["block"] for c in self._ctx.values()]

    def block_ids(self) -> set[str]:
        """지금 실제로 돌고 있는 지점 id 집합.

        ``main.py`` 의 전역 ``BLOCKS`` 대신 이 값을 봐야 한다 — 전역은
        기동 시점의 스냅샷이라, 동적으로 추가·제외된 지점을 반영하지 못한다.
        """
        with self._ctx_lock:
            return set(self._ctx.keys())

    def traffic_flow_samples(self, camera_id: str
                             ) -> tuple[list[tuple[float, float, float, float]],
                                       tuple[int, int] | None] | None:
        """이 카메라의 누적 통행 변위 벡터와, 그 벡터가 어느 해상도
        기준인지(``last_frame_wh``). 상시 처리 중이 아니면 ``None``.

        ★ 2026-08-26 — Phase 4-A(통행 방향 자동 생성) 화면 "자동 생성"
        버튼이 쓴다. ``routes_cameras.py``는 지금까지 DB만 봤는데, 관측
        데이터는 실행 중인 카메라별 추적기(``Perception.tracker``) 안에만
        쌓인다 — 이 메서드가 그 유일한 통로다.

        ⚠️ **잠금은 딕셔너리 조회에만 건다.** ``_ctx`` 자체(삽입·삭제)만
        보호한다는 이 클래스의 기존 원칙(``__init__`` 주석)을 그대로
        따른다 — ``tracker.flow_samples``(``deque``) 읽기는 그 워커
        스레드가 계속 쓰는 중일 수 있지만, 여기서는 스냅샷(제안)이 목적이라
        드물게 섞여 들어가는 값 몇 개는 문제가 되지 않는다.
        """
        with self._ctx_lock:
            c = self._ctx.get(camera_id)
        if c is None:
            return None
        tracker = getattr(c.get("perception"), "tracker", None)
        if tracker is None:
            return None
        try:
            samples = list(getattr(tracker, "flow_samples", None) or [])
        except RuntimeError:  # noqa: BLE001 — 드물게 순회 중 변경(무해, 재시도 안 함)
            samples = []
        return samples, getattr(tracker, "last_frame_wh", None)

    def _start_worker(self, bid: str, c: dict) -> None:
        th = threading.Thread(target=self._block_loop, args=(bid, c), daemon=True)
        c["thread"] = th
        th.start()
        self._workers.append(th)

    def _spawn_block(self, b: dict) -> None:
        """지점 하나를 새로 띄운다(상시 지정이 새로 켜진 경우)."""
        bid = b["id"]
        try:
            c = self._build_block_ctx(b)
        except Exception:  # noqa: BLE001
            # 한 지점 구성 실패가 나머지 지점까지 멈추면 안 된다.
            print(f"[runner] 신규 지점 구성 실패 id={bid} — 건너뜁니다.")
            traceback.print_exc()
            return
        with self._ctx_lock:
            self._ctx[bid] = c
        self._start_worker(bid, c)
        print(f"[runner] 신규 지점 상시 탐지 시작 id={bid}")

    def _retire_block(self, bid: str) -> None:
        """지점 하나를 멈춘다(상시 지정이 꺼졌거나 카메라가 지워진 경우).

        ⚠️ 시계열 추적 상태(``ExpansionRateTracker``·``RiskPredictor``)는
        이 시점에 사라진다 — 다시 켜면 처음부터 다시 쌓는다. 이는 「설정과
        동작이 일치해야 한다」를 위해 감수하는 대가이며, 노면 워처가 이미
        같은 트레이드오프를 지고 있다.
        """
        with self._ctx_lock:
            c = self._ctx.pop(bid, None)
        if c is None:
            return
        c["stop_ev"].set()
        th = c.get("thread")
        if th is not None and th.is_alive():
            # 데몬 스레드라 시간 안에 안 끝나도 프로세스 종료를 막지 않는다.
            th.join(timeout=5.0)
            if th.is_alive():
                print(f"[runner] 경고 — 지점 {bid} 워커가 5초 안에 끝나지 "
                      "않았습니다(다음 틱에 스스로 빠져나옵니다).")
        # 더 이상 아무도 보지 않는 지점의 마지막 스냅샷이 상황판에 남아
        # 있으면, 멈춘 값을 현재 상태로 읽게 된다.
        try:
            self.store.remove(bid)
        except Exception:  # noqa: BLE001
            pass
        print(f"[runner] 지점 상시 탐지 중단 id={bid}")

    def _desired_blocks(self) -> dict[str, dict]:
        """지금 DB 기준으로 **돌아야 하는** 지점 목록.

        ⚠️ 2026-08-23 — 침수·교통위험 둘 중 하나라도 상시면 포함한다
        (``main.py::_load_blocks()`` 와 같은 이유). 실제 침수 판정 실행
        여부는 각 블록의 ``flood_enabled`` 로 따로 가른다(``_block_loop``).
        """
        from ..core import cameras as _cams
        from ..core.db import get_session
        from ..core.roles import Domain as _Dom

        db = get_session()
        try:
            return {b["id"]: b
                    for b in _cams.continuous_blocks_any(
                        db, (_Dom.FLOOD.value, _Dom.TRAFFIC.value))}
        finally:
            db.close()

    def _reconcile(self) -> None:
        """DB의 상시 지정과 실제로 도는 워커를 맞춘다."""
        if not self.dynamic:
            return
        desired = self._desired_blocks()
        current = self.block_ids()
        added = set(desired) - current
        removed = current - set(desired)
        if added or removed:
            # 배포 직후 이 한 줄로 "기존 지점이 대량 정지되지 않았는지"를
            # 곧바로 확인할 수 있어야 한다.
            print(f"[runner] 상시 목록 재조정 — 추가 {len(added)}개 / "
                  f"제외 {len(removed)}개 (현재 {len(current)}개)")
        for bid in removed:
            self._retire_block(bid)
        for bid in added:
            self._spawn_block(desired[bid])

    def _watch_config(self) -> None:
        """``config_rev`` 변경을 기다렸다가 목록을 다시 맞춘다."""
        known = config_rev.revision()
        while not self._stop_ev.is_set():
            known = config_rev.wait_change_or_stop(known, 60.0, self._stop_ev)
            if self._stop_ev.is_set():
                return
            try:
                self._reconcile()
            except Exception:  # noqa: BLE001
                print("[runner] 상시 목록 재조정 실패 — 다음 신호에 다시 봅니다.")
                traceback.print_exc()

    def run(self) -> None:
        # one independent worker thread per camera (block) -> each stream is
        # tracked continuously so ByteTrack ids persist and speed isn't 0
        # (a single round-robin thread is too slow for 4+ streams to
        # accumulate 2 track points, reading 0px/s)
        with self._ctx_lock:
            items = list(self._ctx.items())
        for bid, c in items:
            self._start_worker(bid, c)
        if self.dynamic:
            threading.Thread(target=self._watch_config, daemon=True).start()
        self._stop_ev.wait()

    def _update_flood(self, c, t: float, frame, ps) -> None:
        """Water segmentation -> flood metrics (①②) -> risk (③) -> prediction (④).

        Skipped (last computed value kept) if there's no water model (load
        failed) or no real frame (synthetic source) -- the dashboard keeps
        running either way."""
        if c["water_model"] is None or frame is None:
            return
        every = float(self.water_cfg.get("process_every_seconds", 1.0))
        if t - c["last_water_t"] < every:
            return
        c["last_water_t"] = t
        # H.264 decode-corruption guard -- segmenting a corrupted frame as-is
        # can misclassify decode noise as water (observed in practice). Skip
        # this tick (don't advance the counter) and keep the last good value.
        if is_likely_corrupted_frame(frame):
            c["corrupted_skips"] = c.get("corrupted_skips", 0) + 1
            if c["corrupted_skips"] in (1, 10) or c["corrupted_skips"] % 50 == 0:
                print(f"[runner:flood] treated as corrupted frame, skipping "
                      f"(total {c['corrupted_skips']})")
            return
        c["frame_no"] += 1
        try:
            # ⚠️ **요청값이 아니라 실제로 열린 백엔드**를 봐야 한다.
            #
            #   2026-08-19 에 적재를 파일 판별(auto)로 고쳤는데, **추론 호출부는
            #   그대로 `self._water_backend` 를 보고 있었다.** 그 값은 "auto" 라
            #   어느 쪽과도 같지 않아 **늘 ultralytics 분기**로 떨어졌고,
            #   torchvision 모델에 `.predict` 를 부르다 매 틱 실패했다
            #   (`'LRASPP' object has no attribute 'predict'`).
            #
            # ★ **적재만 고치고 추론을 안 고쳤다.** 「모델이 열렸다」와
            #   「추론이 된다」는 다른 말이다 — models_loaded 는 3 이었지만
            #   실제로는 한 장도 처리하지 못했다.
            # ★ 2026-08-28 — 레터박스(검은 여백)를 뺀 영역만 추론한다.
            #   모델이 여백을 물로 오인하던 문제(실측 오탐 17.9%)를 없앤다.
            #   마스크는 원본 좌표계로 되돌려 받으므로 ROI·증거 상자와
            #   어긋나지 않는다(segment_without_letterbox 주석 참고).
            #   여백이 없는 라이브 CCTV에서는 아무 영향이 없다.
            from ..flood.water_segmentation import segment_without_letterbox
            if getattr(self, "_water_backend_effective", "") == "torchvision":
                # 시맨틱 세그멘테이션 백엔드. conf는 물 클래스 '확률' 임계값이라
                # Ultralytics의 detection conf(0.10)와 의미가 달라 별도 키를 쓴다.
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
            # ★ 증거 팝업이 「이벤트가 발생한 부분」에 쓸 값 (S-88, 2026-08-20).
            #   침수는 개별 탐지가 아니라 영역 세그멘테이션이라, **실제 판정
            #   마스크의 경계**를 상자로 쓴다 — 지어낸 값이 아니다.
            from ..flood.water_segmentation import mask_bbox
            box = mask_bbox(water.mask)
            fh, fw = water.mask.shape[:2]
            boxes = ([{"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3],
                      "label": "침수 영역"}] if box else [])
            # ⚠️ `bid` 는 `_block_loop`(호출부)의 지역변수라 여기서는 안
            #   보인다 — `_update_flood` 는 `(self, c, t, frame, ps)` 만
            #   받는다. `c["block"]["id"]` 가 같은 값이다(2026-08-20 확인,
            #   기동 로그에서 "name 'bid' is not defined" 로 54회 잡았다).
            _evidence.push_boxes(c["block"]["id"], boxes, (fw, fh))

            flood_m = c["flood_engine"].update(
                water, ps.vehicles, ps.persons, c["frame_no"], t)
            risk = c["risk_engine"].score(flood_m)
            flood_m.risk_score, flood_m.risk_grade = risk.risk_score, risk.risk_grade
            pred = c["risk_predictor"].update(flood_m)
            write_back(flood_m, risk, pred)
            # ★ 2026-08-21 확정사항 ③ — 침수 위험도도 독립적으로 SOLAPI
            #   알림을 발생시킨다(예전엔 화면 표시용일 뿐이었다).
            #   발동 등급은 코드에 박지 않고 관리자가 S-95 화면에서 바꾼다
            #   (core.settings.flood_notify_min_grade) — 기본값 4는 개발사
            #   판단이라 기관마다 조정이 필요할 수 있다.
            _notify_flood_risk(self._notifier, flood_m, risk, c["block"]["name"],
                               min_grade=ug_settings.flood_notify_min_grade())
            # ★ 2026-08-21 flood/traffic 도메인 분리: `stopped_vehicles_near_water`
            #   ("정지"=교통행동 + "물 근처"=위치 결합값)는 이제 FloodMetrics에
            #   없다. 화면 표시용으로만 쓰이던 이 값은, 두 도메인의 산출물을
            #   가진 orchestrator(여기)가 직접 조합해 계산한다.
            stopped_near_water = sum(
                1 for v in ps.vehicles
                if getattr(v, "stalled", False)
                and point_on_mask(bottom_center(v.bbox), water.mask, pad=8)
            )
            c["last_flood"] = (flood_m, risk, pred, stopped_near_water)
        except Exception as e:  # noqa: BLE001
            print(f"[runner:flood] {str(e)[:120]}")

    def _block_loop(self, bid, c) -> None:
        # 전체 정지(self._stop_ev) 또는 이 지점만 정지(c["stop_ev"]) —
        # 후자는 _retire_block 이 세운다. 예전 컨텍스트(stop_ev 없음)도
        # 받아들이도록 get() 으로 읽는다.
        own_stop = c.get("stop_ev")
        while not self._stop_ev.is_set() and not (own_stop and own_stop.is_set()):
            # ⚠️ ROI 재조회는 **별도 try** 로 감싼다 — 아래 큰 try 안에 두면,
            #   재조회 실패 하나가 이번 틱 전체(프레임 처리·위험도 계산)를
            #   건너뛰게 만든다(바로 아래 except 주석이 설명하는 "한 오류가
            #   전체 틱을 죽이는" 것과 같은 함정). DB 조회는 실제로 저장
            #   버튼이 눌린 드문 순간에만 일어난다 — config_rev.revision()
            #   비교 자체는 매 틱 해도 비용이 사실상 0이다.
            # ⚠️ 2026-08-23 — 침수가 미사용인 블록(교통위험만 상시)은 ROI
            # 재조회 자체를 건너뛴다. 안 건너뛰면 "탐지 지정을 껐는데도
            # 로그에 ROI를 계속 다시 읽는다"는 혼란을 준다 — 아래
            # _update_flood() 호출도 어차피 건너뛰어 결과가 쓰이지 않는다.
            if c["block"].get("flood_enabled"):
                try:
                    rev = config_rev.revision()
                    if rev != c.get("roi_rev"):
                        new_roi = load_roi_for_camera(
                            bid, "flood",
                            fallback_path=PROJECT_ROOT / "configs" / "roi" / f"{bid}.json")
                        c["roi"] = new_roi
                        c["flood_engine"].set_roi(new_roi)
                        c["roi_rev"] = rev
                        print(f"[runner:{bid}] ROI 다시 읽음 (rev={rev})")
                except Exception:  # noqa: BLE001
                    print(f"[runner:{bid}] ROI 재조회 실패 — 기존 ROI로 계속합니다.")
                    traceback.print_exc()
            try:
                # ★ 2026-08-29 — 재배포가 죽은 채로 방치돼도(재부팅 후
                # MediaMTX가 자동 복구되지 않는 결함 등) 서비스 재기동 없이
                # 스스로 회복하도록 5분마다 재연결을 시도한다. _next_frame
                # 보다 먼저 불러야, 이번 틱에 회복이 확인되면 그 즉시 실제
                # 프레임을 받는다(_retry_restream_if_due 문서 참고).
                self._retry_restream_if_due(c)
                t, dets, frame = self._next_frame(c)
                # ★ 2026-08-28 — CCTV 재배포 허브. kind가 "restream_unavailable"
                # 이면 이번 틱의 frame/dets는 SyntheticDetectionSource가 만든
                # 대체물일 뿐 실제 영상이 아니다 — 아래 두 게이트에서 침수·
                # 교통위험 판정에 쓰이지 않도록 막는다(폴백 없음 정책).
                # 하천수위(river_agent)는 카메라 영상과 무관한 별도 계측
                # 값이라 이 상태와 무관하게 계속 판정한다.
                # ⚠️ .get() 필수 — "kind"가 없는 ctx(단위시험이 손으로 만든
                # 컨텍스트 등)에서 c["kind"]로 접근하면 KeyError가 나고, 그
                # 예외는 이 함수 맨 아래의 포괄 except가 삼켜 버려 dt=0.0인
                # while 루프가 무한히 돈다(실측 발견, 2026-08-28 — 회귀
                # 시험 하나가 이 때문에 멈추지 않고 계속 돌았다).
                restream_down = (c.get("kind") == "restream_unavailable")
                # 증거 링 버퍼(S-88). 이벤트는 상황이 벌어진 뒤에 뜨므로
                # 「직전 장면」은 미리 담아 두어야 남길 수 있다.
                if frame is not None and not restream_down:
                    _evidence.push(bid, frame)
                ps = c["perception"].step(t, dets, c["wh"])  # ① perception: update object attributes
                weather, metrics = ps.weather, ps.metrics
                river = c["river"].at(t)
                # ⚠️ 2026-08-23 — 침수가 미사용인 블록(예: 교통위험만 상시로
                # 켠 카메라)에서는 침수 판정을 아예 돌리지 않는다. 이 게이트가
                # 없으면 "탐지 지정을 껐는데도" 보관 중인 예전 ROI로
                # 물 세그멘테이션이 계속 돌고, 심하면 침수 알림까지 나갈 수
                # 있었다 — 실사용 중 발견(교통위험 상시 카메라가 실시간 관제
                # 화면에 안 보이는 문제를 조사하다 함께 확인).
                if c["block"].get("flood_enabled") and not restream_down:
                    self._update_flood(c, t, frame, ps)  # flood risk (measurement/prediction)
                img = None
                # ★ 2026-08-28 — 재배포 서버 응답 없음이면 스냅샷 갱신을
                # 건너뛴다. frame/dets가 SyntheticDetectionSource가 만든
                # 대체물이라, 여기서 새로 만들면 지어낸 장면을 실제 CCTV
                # 스냅샷인 것처럼 화면 카드에 내보내게 된다 — `c["last_b64"]`
                # 는 그대로 두어 장애 직전 마지막 실제 스냅샷이 남는다.
                if t - c["last_snap_t"] >= 1.0 and not restream_down:  # ~1s
                    img = (frame_to_image(frame, dets) if frame is not None
                           else render_scene(dets, weather, c["wh"]))
                    c["last_b64"] = to_jpeg_b64(img)
                    c["last_snap_t"] = t
                # ★ 2026-08-28 — 교통위험 미지정 카메라(침수만 상시)에서는
                # VLM 호출·semantic·decider를 아예 돌리지 않는다. 바로 위
                # perception.step()은 계속 돈다 — 침수가 차량·보행자 위치를
                # 그 결과에서 가져오기 때문이다(flood_engine.update()는
                # 위치만 쓰고 ByteTrack 추적 결과·stopped_near_water는
                # 쓰지 않는다). 이 게이트가 없으면 교통 미지정 카메라에서도
                # VLM API 호출과 SOLAPI 교통위험 알림이 조용히 나갈 수
                # 있었다(실사용 점검 중 발견 — docs/pending_tasks.md
                # 2026-08-28 항목). `situation`/`decision`은 `_snapshot()`이
                # 반드시 필요로 하므로, 판정을 안 도는 대신 중립값을 채운다
                # (`_no_traffic_decision` — 지어낸 판정이 아니라는 것은
                # risk_code=""와 traffic_enabled=False로 구분된다).
                if c["block"].get("traffic_enabled") and not restream_down:
                    situation = c["vlm"].interpret(img, metrics, weather,
                                                   location=c["block"]["name"])
                    # ★ 2026-08-21 flood/traffic 도메인 분리: semantic(교통·기상)은
                    #   이제 하천수위를 받지 않는다 — 순수 교통·기상 판정만 한다.
                    risk = c["semantic"].infer(weather, metrics, situation,
                                               context={"block": c["block"]["name"]})
                    decision = c["decider"].decide(risk, t)
                    # ★ 2026-08-26: `decision.alert`(생성 시점에 고정된 alert_min=2)
                    #   대신 **매 틱 설정을 새로 읽는다** — 관리자가 S-95 화면에서
                    #   바꾸면 재기동 없이 다음 판정부터 반영된다(flood_notify_min_grade
                    #   와 같은 방식).
                    if decision.severity >= ug_settings.traffic_notify_min_severity():
                        _notify_decision(self._notifier, decision, c["block"]["name"])
                else:
                    situation = {}
                    decision = _no_traffic_decision(t)
                # ★ 하천수위(직접 침수 신호)는 FloodRiverAgent가 완전히 독립적으로
                #   판정한다. risk_code 네임스페이스(FR_*)가 교통 쪽(TWR_*)과
                #   겹치지 않아, 두 알림이 같은 틱에 각각 독립적으로 나갈 수 있다
                #   (확정사항 ②).
                raining = weather.intensity != WeatherIntensity.none
                river_risk = c["river_agent"].infer(
                    river, raining=raining, context={"block": c["block"]["name"]})
                river_decision = c["river_decider"].decide(river_risk, t)
                if river_decision.alert:
                    _notify_decision(self._notifier, river_decision, c["block"]["name"])
                # VLM을 부르지 않은 틱은 last_source도 지어내지 않는다
                # ("한 번도 안 불렀다"는 것을 그대로 남긴다) — 이유가
                # "교통 미지정"인지 "재배포 서버 응답 없음"인지 구분해 둔다.
                if restream_down:
                    vlm_src = "미지정(재배포 서버 응답 없음)"
                elif c["block"].get("traffic_enabled"):
                    vlm_src = c["vlm"].last_source
                else:
                    vlm_src = "미지정(교통 미사용)"
                self.store.update(bid, _snapshot(
                    c["block"], weather, metrics, situation, decision, river,
                    c["last_b64"], vlm_src, c["kind"], ps.vehicles,
                    flood=c["last_flood"], incidents=ps.incidents,
                    corrupted_skips=c.get("corrupted_skips", 0),
                    quality_grade=_video_quality.grade_for(bid)))
            except Exception as e:  # noqa: BLE001
                # ⚠️ **한 줄만 찍으면 원인을 못 찾는다.**
                #
                #   2026-08-20 전체 점검에서 이 자리의 오류가
                #   **54,515회** 쌓여 있는 것을 찾았다. 매 틱마다 루프 본문이
                #   통째로 실패해 **perception·위험도·스냅샷이 하나도 갱신되지
                #   않고 있었다.** 그런데 남은 것은 잘린 메시지 한 줄뿐이라
                #   **어디서 났는지 알 수 없었다.**
                #
                # ★ 그래서 **블록마다 첫 한 번은 트레이스백을 남긴다.**
                #   매번 남기면 로그가 터지고(실제로 54,515줄이었다), 안 남기면
                #   원인을 영영 못 찾는다. 첫 번째만 남기는 것이 그 사이다.
                # ⚠️ 이 print들 자체가 실패하면 안 된다. 2026-08-29 실사용
                #   중 발견 — Windows 콘솔(cp949)이나 그 인코딩을 물려받은
                #   리다이렉트 환경에서는, 메시지에 em-dash("—") 같은
                #   cp949 밖 문자가 하나만 있어도 print()가
                #   ``UnicodeEncodeError``를 던진다. 여기는 **이미 예외를
                #   처리하던 except 블록 안**이라 그 새 예외를 잡아 줄
                #   바깥 try/except가 없다 — 로그를 남기려던 시도가 오히려
                #   블록 루프 스레드 전체를 조용히 죽인다(그러면 이
                #   카메라는 서비스 재기동 전까지 다시는 관측되지 않는다 —
                #   위의 "54,515회" 사례보다 훨씬 나쁜 결과다). 그래서 이
                #   진단 출력 자체를 또 한 번 try로 감싸, 인코딩이 안 되면
                #   ASCII만으로라도 "여기서 오류가 났다"는 사실만은 남긴다.
                try:
                    if not c.get("err_traced"):
                        c["err_traced"] = True
                        print(f"[runner:{bid}] 첫 오류 — 아래 트레이스백은 "
                              f"이 지점에서 한 번만 남깁니다.")
                        traceback.print_exc()
                    print(f"[runner:{bid}] {str(e)[:120]}")
                except Exception:  # noqa: BLE001
                    print(f"[runner:{bid}] error (log message encoding failed)")
            self._stop_ev.wait(self.dt)

    def stop(self) -> None:
        self._stop_ev.set()
