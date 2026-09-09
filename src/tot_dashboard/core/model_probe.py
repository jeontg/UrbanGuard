"""모델 시험 탐지 — 모델을 골라 한 지점에 대 보는 시험대 (S-61).

무엇을 푸는가
    노면 모델은 지금까지 4번 학습해 후보가 여러 개인데, **둘을 같은 CCTV에
    대 보려면 코드를 고쳐 서비스를 재기동하는 수밖에** 없었습니다. 그래서
    「이 모델이 저 모델보다 나은가」를 화면에서 답할 수 없었고, 결국 판단이
    학습 로그의 mAP 숫자에만 의존했습니다. 그 숫자가 부산 CCTV에서의 성능과
    다르다는 것이 이미 드러났는데도요.

    이 모듈은 **같은 대상에 서로 다른 모델을 차례로 대 보고 결과를 눈으로
    비교**하게 합니다.

운영과 분리한 점 — 중요합니다
    * 여기서 나온 결과는 **이벤트·노면 현황에 기록하지 않습니다.** 시험은
      시험이고, 관제 화면에 시험 결과가 섞이면 그 화면을 믿을 수 없게 됩니다.
    * 여기서 고른 모델은 **상시 탐지에 반영되지 않습니다.** 반영하려면 워처
      재구성이 필요해 별도 작업입니다. 화면에도 그렇게 적어 둡니다.

개인정보
    미리보기 이미지는 **사람을 가린 뒤에만** 저장합니다. 가리지 못하면
    저장하지 않습니다 — 학습 프레임 수집(``road/dataset_collector.py``)과 같은
    원칙입니다.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..common.config import PROJECT_ROOT
from . import inference
from . import model_registry as registry

log = logging.getLogger("urbanguard.model_probe")

PREVIEW_DIR = PROJECT_ROOT / "data" / "model_probe"
# 미리보기는 시험 흔적이라 오래 둘 이유가 없다. 이 수를 넘으면 오래된 것부터 지운다.
PREVIEW_KEEP = 60

DEFAULT_DURATION_SEC = 8.0
DEFAULT_INTERVAL_SEC = 1.5
MAX_DURATION_SEC = 30.0
DEFAULT_VIDEO_FRAMES = 8

# 추론은 CPU를 통째로 먹는다. 두 개가 동시에 돌면 둘 다 느려지고 스트림도
# 놓친다. 한 번에 하나만 돌린다.
_lock = threading.Lock()


@dataclass
class ProbeResult:
    domain: str
    model_key: str
    model_label: str
    backend: str
    target_id: str
    target_name: str
    kind: str = "cctv"                     # cctv | video
    frames_analyzed: int = 0
    elapsed_sec: float = 0.0
    ok: bool = False
    note: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    preview: str = ""                      # 미리보기 파일명 (없으면 빈 문자열)
    preview_note: str = ""
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "domain": self.domain, "model_key": self.model_key,
            "model_label": self.model_label, "backend": self.backend,
            "backend_label": registry.BACKEND_LABELS.get(self.backend,
                                                         self.backend),
            "target_id": self.target_id, "target_name": self.target_name,
            "kind": self.kind, "frames_analyzed": self.frames_analyzed,
            "elapsed_sec": round(self.elapsed_sec, 1), "ok": self.ok,
            "note": self.note, "metrics": self.metrics,
            "preview": self.preview, "preview_note": self.preview_note,
            "ran_at": self.ran_at.isoformat(),
        }


# --- 대상 목록 ---------------------------------------------------------------
def targets(domain: str) -> list[dict]:
    """이 도메인에서 시험해 볼 수 있는 대상 — 등록 CCTV와 동영상 파일.

    DB를 못 읽어도 빈 목록을 돌려줄 뿐 예외를 올리지 않는다. 모델 화면이
    DB 문제로 통째로 안 뜨면 곤란하다.
    """
    out: list[dict] = []
    try:
        from . import cameras as _cams
        from .db import get_session
        db = get_session()
        try:
            for cam in _cams.for_domain(db, domain):
                if not cam.is_active:
                    continue
                url, kind = _source_of(cam)
                if not url:
                    continue
                out.append({"id": cam.id, "name": cam.name, "kind": kind,
                            "note": "등록 동영상" if kind == "video" else "실시간 CCTV"})
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        log.warning("시험 대상 카메라 조회 실패 domain=%s: %s", domain, str(e)[:120])

    # 노면은 업로드해 둔 학습·시험용 동영상도 대상이 된다.
    if domain == "road":
        try:
            from ..road.live_analyzer import list_available_videos
            for v in list_available_videos():
                out.append({"id": v["id"], "name": v.get("name", v["id"]),
                            "kind": "video", "note": "업로드 동영상"})
        except Exception as e:  # noqa: BLE001
            log.warning("노면 동영상 목록 조회 실패: %s", str(e)[:120])
    return out


def _source_of(cam) -> tuple[str, str]:
    """카메라에서 (열 수 있는 주소, 종류)를 뽑는다."""
    if cam.source_type == "video" and cam.source_path:
        p = Path(cam.source_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return (str(p) if p.is_file() else "", "video")
    if cam.source_url:
        return cam.source_url, "cctv"
    return "", ""


# --- 프레임 수집 -------------------------------------------------------------
def _grab_frames(url: str, *, duration_sec: float, interval_sec: float,
                 max_frames: int) -> tuple[list[np.ndarray], str]:
    """스트림·파일에서 표본 프레임을 뽑는다. (프레임 목록, 실패 사유)

    ⚠️ 같은 카메라를 상시 탐지가 이미 보고 있으면 CCTV 서버가 세 번째 연결을
    거절한다(실측). 한 번은 쉬었다 다시 붙어 본다 — ``road/live_analyzer`` 가
    같은 이유로 같은 처리를 한다.
    """
    import cv2

    frames: list[np.ndarray] = []
    opened = False
    for attempt in (1, 2):
        cap = cv2.VideoCapture(url)
        opened = cap.isOpened()
        if opened:
            deadline = time.time() + max(duration_sec, 0.5)
            loop_t0 = time.time()
            next_sample = 0.0
            while time.time() < deadline and len(frames) < max_frames:
                ok, fr = cap.read()
                if not ok or fr is None:
                    # 동영상 파일은 끝나면 더 읽을 것이 없다. 무한 대기하지 않는다.
                    if frames:
                        break
                    time.sleep(0.05)
                    if time.time() >= deadline:
                        break
                    continue
                elapsed = time.time() - loop_t0
                if elapsed >= next_sample:
                    frames.append(fr)
                    next_sample = elapsed + interval_sec
        cap.release()
        if frames or attempt == 2:
            break
        time.sleep(1.5)

    if frames:
        return frames, ""
    why = ("연결이 거부되었습니다. 같은 카메라를 상시 탐지가 보고 있으면 "
           "동시 접속이 막힐 수 있습니다."
           if not opened else "연결은 됐지만 화면을 받지 못했습니다.")
    return [], why


# --- 미리보기 저장 -----------------------------------------------------------
def _save_preview(img: np.ndarray) -> tuple[str, str]:
    """사람을 가린 뒤 미리보기를 저장한다. (파일명, 안내문)

    가리지 못하면 **저장하지 않는다.** 시험 편의를 위해 개인정보 보호를
    양보하지 않는다.
    """
    import cv2

    from . import image_mask
    try:
        status, _n = image_mask.mask_array(img)
    except Exception as e:  # noqa: BLE001
        log.warning("미리보기 마스킹 실패: %s", str(e)[:120])
        return "", "사람을 가리지 못해 미리보기를 저장하지 않았습니다."
    if status in image_mask.NEEDS_REVIEW:
        return "", ("사람을 가릴 수 없어(검출기 사용 불가) 미리보기를 "
                    "저장하지 않았습니다.")
    try:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.jpg"
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            return "", "미리보기 인코딩에 실패했습니다."
        # ⚠️ cv2.imwrite 는 한글이 든 경로에서 조용히 실패한다. 인코딩과 쓰기를
        # 나눠 파이썬이 파일을 쓰게 한다(과거에 실제로 겪은 문제).
        (PREVIEW_DIR / name).write_bytes(buf.tobytes())
        _trim_previews()
        return name, ""
    except Exception as e:  # noqa: BLE001
        log.warning("미리보기 저장 실패: %s", str(e)[:120])
        return "", "미리보기를 저장하지 못했습니다."


def _trim_previews() -> None:
    try:
        files = sorted(PREVIEW_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for p in files[:-PREVIEW_KEEP]:
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# --- 도메인별 추론 -----------------------------------------------------------
def _probe_road(model: registry.ModelInfo, target_id: str, kind: str,
                *, duration_sec: float, conf: float) -> ProbeResult:
    """노면은 기존 분석기가 모델 경로 주입을 이미 지원한다 — 그대로 쓴다."""
    from ..road.live_analyzer import RoadDefectAnalyzer

    analyzer = RoadDefectAnalyzer(model_path=model.key, conf=conf)
    mode = "video" if kind == "video" else "cctv"
    block = None
    if mode == "cctv":
        block = _block_of(target_id)

    # 분석기가 뽑은 프레임을 그대로 받아 둔다. 스트림을 따로 또 열면 같은
    # 카메라에 두 번 붙는 셈이라 CCTV 서버가 거절한다(실측).
    grabbed: list = []

    def _sink(frames):
        grabbed.extend(fr for _idx, fr in frames)

    res = analyzer.analyze(mode=mode, target=target_id, conf=conf,
                           duration_sec=duration_sec, block=block,
                           frame_sink=_sink)
    d = res.to_dict()

    preview, pnote = ("", "")
    if grabbed:
        img = _annotate_road(model, grabbed, conf)
        if img is not None:
            preview, pnote = _save_preview(img)

    return ProbeResult(
        domain="road", model_key=model.key, model_label=model.label,
        backend=model.backend, target_id=target_id,
        target_name=d.get("target_name") or target_id, kind=kind,
        frames_analyzed=d.get("frames_analyzed", 0),
        elapsed_sec=d.get("elapsed_sec", 0.0),
        ok=bool(d.get("frames_analyzed")),
        note=d.get("note", ""),
        preview=preview, preview_note=pnote,
        metrics={
            "탐지 건수": d.get("defect_count", len(d.get("defects", []) or [])),
            "등급": d.get("grade_label") or "—",
        })


def _annotate_road(model: registry.ModelInfo, frames: list, conf: float):
    """탐지 상자를 그린 프레임을 고른다. 탐지가 0건이어도 화면은 돌려준다.

    0건일 때 아무것도 안 보여 주면 **「손상이 없다」와 「도로가 안 보인다」를
    구분할 수 없습니다.** 부산 CCTV는 각도에 따라 노면이 거의 안 나오는 지점이
    있어, 그 구분이 판단의 절반입니다.
    """
    try:
        net = _open(model)
        best_img, best_n = None, -1
        for fr in frames:
            r = inference.predict(net, fr, conf=conf)[0]
            n = len(r.boxes) if r.boxes is not None else 0
            if n > best_n:
                best_n, best_img = n, r.plot()
        return best_img
    except Exception as e:  # noqa: BLE001
        log.warning("노면 미리보기 생성 실패: %s", str(e)[:120])
        return frames[0] if frames else None


def _open(model: registry.ModelInfo):
    """시험용으로 모델을 연다. **task 를 레지스트리에서 받아 못박는다.**

    ``.onnx`` 나 OpenVINO 산출물은 task 정보를 잃을 수 있어, 분할 모델이
    검출로 열리면 **마스크 계수를 클래스 점수로 읽습니다**(신뢰도가 1을 넘고
    상자가 300개씩 나옵니다). 시험 탐지는 「이 모델이 쓸 만한가」를 판단하는
    자리라, 그런 상태로 나온 결과를 보면 **멀쩡한 모델을 버리게 됩니다.**

    예열은 하지 않습니다 — 시험은 프레임 몇 장이라 예열이 추론보다 오래
    걸립니다(OpenVINO 기준 약 27초). 여기서 중요한 것은 속도가 아니라
    **결과가 맞는가**입니다.
    """
    return inference.load(registry.resolve(model.key),
                          task=inference.task_of(model.backend), warmup=False)


def _block_of(camera_id: str):
    try:
        from . import cameras as _cams
        from .db import get_session
        db = get_session()
        try:
            cam = _cams.get(db, camera_id)
            return _cams.to_block_dict(cam) if cam is not None else None
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return None


def _probe_crowd(model: registry.ModelInfo, frames: list, target_name: str,
                 target_id: str, kind: str, *, conf: float) -> dict:
    """사람 수를 센다. 프레임별 인원수의 평균·최대를 준다."""
    net = _open(model)
    counts: list[int] = []
    best_img = None
    best_n = -1
    for fr in frames:
        r = inference.predict(net, fr, conf=conf)[0]
        names = getattr(r, "names", {}) or {}
        n = 0
        for b in (r.boxes or []):
            cls = int(b.cls[0]) if b.cls is not None else -1
            if str(names.get(cls, "")).lower() == "person":
                n += 1
        counts.append(n)
        if n > best_n:
            best_n, best_img = n, r.plot()
    return {
        "metrics": {"평균 인원": round(sum(counts) / len(counts), 1) if counts else 0,
                    "최대 인원": max(counts) if counts else 0,
                    "프레임별": ", ".join(str(c) for c in counts)},
        "preview_img": best_img,
    }


_VEHICLE_NAMES = ("car", "truck", "bus", "motorcycle")


def _probe_traffic(model: registry.ModelInfo, frames: list, target_name: str,
                   target_id: str, kind: str, *, conf: float) -> dict:
    """차량 수를 센다 (2026-08-21 flood/traffic 도메인 분리로 신설).

    ⚠️ **속도 추정은 하지 않는다.** 1차 구현은 대수만 본다 — 속도(km/h)를
    내려면 프레임 간 추적과 지면 평면 보정이 필요한데, 그건 별도 과제다
    (docs/202608210801 9절 확인사항 1번). 여기서 "평균 속도"를 지어내면
    보정하지 않은 픽셀 값을 실제 속도처럼 보여 주게 된다.
    """
    net = _open(model)
    counts: list[int] = []
    best_img = None
    best_n = -1
    for fr in frames:
        r = inference.predict(net, fr, conf=conf)[0]
        names = getattr(r, "names", {}) or {}
        n = 0
        for b in (r.boxes or []):
            cls = int(b.cls[0]) if b.cls is not None else -1
            if str(names.get(cls, "")).lower() in _VEHICLE_NAMES:
                n += 1
        counts.append(n)
        if n > best_n:
            best_n, best_img = n, r.plot()
    return {
        "metrics": {"평균 차량": round(sum(counts) / len(counts), 1) if counts else 0,
                    "최대 차량": max(counts) if counts else 0,
                    "프레임별": ", ".join(str(c) for c in counts)},
        "preview_img": best_img,
    }


def _probe_flood(model: registry.ModelInfo, frames: list, *, conf: float) -> dict:
    """수면 비율을 잰다. 백엔드에 따라 로드·추론 방식이 다르다."""
    import cv2

    ratios: list[float] = []
    best_img = None
    best_ratio = -1.0

    if model.backend == registry.TV_SEG:
        from ..flood.water_segmentation_tv import (load_tv_water_model,
                                                   segment_water_tv)
        net = load_tv_water_model(registry.resolve(model.key))
        run = lambda fr: segment_water_tv(net, fr, conf=conf)  # noqa: E731
    else:
        from ..flood.water_segmentation import segment_water
        net = _open(model)
        run = lambda fr: segment_water(net, fr, conf=conf)  # noqa: E731

    for fr in frames:
        res = run(fr)
        h, w = fr.shape[:2]
        ratio = res.water_pixels / float(h * w) * 100.0 if h and w else 0.0
        ratios.append(ratio)
        if ratio > best_ratio:
            best_ratio = ratio
            # 물 영역을 파랗게 덧칠해 눈으로 확인할 수 있게 한다.
            overlay = fr.copy()
            mask = res.mask.astype(bool)
            overlay[mask] = (0.45 * overlay[mask] +
                             0.55 * np.array([255, 120, 0])).astype(np.uint8)
            best_img = cv2.addWeighted(fr, 0.35, overlay, 0.65, 0)

    return {
        "metrics": {"평균 수면 비율(%)": round(sum(ratios) / len(ratios), 2) if ratios else 0.0,
                    "최대 수면 비율(%)": round(max(ratios), 2) if ratios else 0.0,
                    "프레임별(%)": ", ".join(f"{r:.1f}" for r in ratios)},
        "preview_img": best_img,
    }


# --- 진입점 -----------------------------------------------------------------
def run(domain: str, model_key: str, target_id: str, *, kind: str = "cctv",
        duration_sec: float = DEFAULT_DURATION_SEC,
        conf: float = 0.25) -> ProbeResult:
    """모델 하나를 대상 하나에 대 본다.

    동시에 하나만 돈다. 이미 돌고 있으면 :class:`RuntimeError` 를 낸다 —
    호출부가 409 로 바꿔 「잠시 후 다시」를 안내한다.
    """
    model = registry.get(model_key)
    if model is None:
        raise LookupError(f"모델을 찾을 수 없습니다: {model_key}")
    if not model.exists:
        raise FileNotFoundError(f"모델 파일이 없습니다: {model.key}")
    # ★ 2026-08-21: 리터럴 목록 대신 enum 을 쓴다 — 도메인을 늘리고 여기를
    #   잊으면 시험 탐지가 통째로 막힌다.
    from . import roles as _roles
    if domain not in {d.value for d in _roles.Domain}:
        raise ValueError(f"알 수 없는 도메인입니다: {domain}")
    # ⚠️ 화면 드롭다운은 도메인별로 걸러서 보여줄 뿐 방어선이 아니다 — 폼을
    #   조작하면 노면 모델을 domain="flood" 로 시험할 수 있었다(2026-08-22
    #   전수점검, model_ops.choose() 와 같은 종류의 결함). 분류 미상("")은
    #   통과시킨다.
    if model.domain not in (domain, ""):
        raise ValueError(f"이 모델은 「{model.domain}」 전용입니다 — "
                         f"「{domain}」 도메인 시험탐지에는 쓸 수 없습니다: {model.key}")

    duration_sec = max(1.0, min(float(duration_sec), MAX_DURATION_SEC))

    if not _lock.acquire(blocking=False):
        raise RuntimeError("다른 시험 탐지가 진행 중입니다. 잠시 후 다시 시도하십시오.")
    try:
        t0 = time.time()
        if domain == "road":
            res = _probe_road(model, target_id, kind,
                              duration_sec=duration_sec, conf=conf)
            res.elapsed_sec = res.elapsed_sec or (time.time() - t0)
            return res

        url, name = _resolve_target(domain, target_id)
        if not url:
            return ProbeResult(
                domain=domain, model_key=model.key, model_label=model.label,
                backend=model.backend, target_id=target_id, target_name=name,
                kind=kind, elapsed_sec=time.time() - t0,
                note="이 대상은 실시간 스트림도 동영상 파일도 없습니다. "
                     "CCTV 관리(S-80)에서 소스를 확인하십시오.")

        frames, why = _grab_frames(
            url, duration_sec=duration_sec, interval_sec=DEFAULT_INTERVAL_SEC,
            max_frames=DEFAULT_VIDEO_FRAMES)
        if not frames:
            return ProbeResult(
                domain=domain, model_key=model.key, model_label=model.label,
                backend=model.backend, target_id=target_id, target_name=name,
                kind=kind, elapsed_sec=time.time() - t0, note=why)

        try:
            # ★ 2026-08-21: 예전에는 「crowd 가 아니면 무조건 flood」였다.
            #   그 상태로 도메인을 늘리면 **교통 모델을 물 세그멘테이션으로
            #   조용히 돌려** 그럴듯한 숫자를 내놓는다 — 사람이 오판한다.
            #   그래서 명시적 분기로 바꾸고, 모르는 도메인은 터뜨린다.
            if domain == "crowd":
                out = _probe_crowd(model, frames, name, target_id, kind, conf=conf)
            elif domain == "traffic":
                out = _probe_traffic(model, frames, name, target_id, kind, conf=conf)
            elif domain == "flood":
                out = _probe_flood(model, frames, conf=conf)
            else:
                raise ValueError(f"이 도메인의 시험 탐지 방법이 없습니다: {domain}")
        except Exception as e:  # noqa: BLE001
            log.exception("시험 추론 실패 domain=%s model=%s", domain, model.key)
            return ProbeResult(
                domain=domain, model_key=model.key, model_label=model.label,
                backend=model.backend, target_id=target_id, target_name=name,
                kind=kind, frames_analyzed=len(frames),
                elapsed_sec=time.time() - t0,
                note=f"추론에 실패했습니다. 모델과 추론 방식이 맞는지 "
                     f"확인하십시오 — {type(e).__name__}: {str(e)[:160]}")

        preview, pnote = ("", "")
        if out.get("preview_img") is not None:
            preview, pnote = _save_preview(out["preview_img"])

        return ProbeResult(
            domain=domain, model_key=model.key, model_label=model.label,
            backend=model.backend, target_id=target_id, target_name=name,
            kind=kind, frames_analyzed=len(frames),
            elapsed_sec=time.time() - t0, ok=True,
            metrics=out["metrics"], preview=preview, preview_note=pnote)
    finally:
        _lock.release()


def _resolve_target(domain: str, target_id: str) -> tuple[str, str]:
    try:
        from . import cameras as _cams
        from .db import get_session
        db = get_session()
        try:
            cam = _cams.get(db, target_id)
            if cam is None:
                return "", target_id
            url, _kind = _source_of(cam)
            return url, cam.name
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        log.warning("시험 대상 조회 실패 %s: %s", target_id, str(e)[:120])
        return "", target_id


def busy() -> bool:
    """지금 시험 탐지가 돌고 있는가. 화면이 버튼을 잠그는 데 쓴다."""
    if _lock.acquire(blocking=False):
        _lock.release()
        return False
    return True
