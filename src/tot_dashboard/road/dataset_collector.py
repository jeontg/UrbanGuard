"""노면 학습 데이터 자동 수집 (Phase 3).

RDD2022로 학습한 모델은 부산 CCTV에서 **탐지 0건**이다. 50 에폭까지 늘려도,
정반대 조건(근접 촬영)에서도 0건이었다 — 학습량이 아니라 **촬영 조건이 다른
도메인 갭** 문제다(``docs/road_surface_management_plan.md`` Phase 2). 실제
배포 환경에서 찍힌 자체 데이터로 학습하는 것 말고는 길이 없다.

그 첫 단계가 프레임 확보다. ``scripts/collect_road_cctv_frames.py`` 가 이미
있지만 **사람이 기억해서 실행해야** 하고, 그래서 지금까지 28장 모였다. 상시
순회가 어차피 15분마다 프레임을 뽑고 있으니 그때 함께 남기면 시간대·날씨가
자연히 섞인 데이터가 쌓인다.

스트림을 따로 열지 않는다
    같은 카메라에 세 번째로 붙으면 CCTV 서버가 연결을 거절한다(실측). 그래서
    수집기는 자기 스트림을 열지 않고, **분석기가 이미 뽑아 둔 프레임을 받아**
    저장한다.

기본은 꺼짐
    이 기능은 도로 영상을 디스크에 계속 쌓는다. 켜는 것은 운영 판단이어야지
    기본값이어서는 안 된다. 관리자가 화면에서 켠다.

마스킹에 실패하면 저장하지 않는다
    ROI 작업용 정지영상(``core/snapshot.py``)은 마스킹이 실패해도 화면에
    띄운다 — 한 번 보고 버리는 화면이라 그래야 작업이 성립한다. 학습 데이터는
    반대다. **오래 보관하는 파일**이므로 가리지 못했으면 남기지 않는다.

⚠️ 한계 — 사람은 가리지만 **차량번호판은 전용 모델이 없어 가리지 못한다**
(``core/image_mask.py`` 와 같은 한계). 수집한 프레임을 외부로 내보내기 전에
육안 확인이 필요하며, ``meta.jsonl`` 에 마스킹 상태를 함께 남긴다.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..common.config import PROJECT_ROOT

log = logging.getLogger("urbanguard.road_dataset")

# 기존 수집 스크립트와 **같은 폴더**를 쓴다. 라벨링 도구
# (``scripts/label_road_defects.py``)가 이 경로를 그대로 읽으므로,
# 자동 수집분과 수동 수집분을 따로 관리할 이유가 없다.
DATASET_DIR = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "raw"
META_NAME = "meta.jsonl"

# 한 번의 관측에서 남길 장수. 같은 15초 창에서 뽑은 프레임은 서로 거의 같아
# 많이 남겨도 학습에 보탬이 되지 않는다.
MAX_PER_ROUND = int(os.environ.get("URBANGUARD_ROAD_COLLECT_PER_ROUND", "2"))
# 지점당 하루 상한. 상시 순회는 15분마다 도니 상한이 없으면 하루 96회 × 2장이
# 쌓인다. 그렇게 모은 프레임은 대부분 서로 비슷해 라벨링 비용만 늘린다.
MAX_PER_DAY = int(os.environ.get("URBANGUARD_ROAD_COLLECT_PER_DAY", "8"))
# 같은 지점을 다시 담기까지의 최소 간격. 집중 감시(60초 주기)가 켜져 있어도
# 같은 장면이 연달아 쌓이지 않게 한다.
MIN_INTERVAL_SEC = float(os.environ.get("URBANGUARD_ROAD_COLLECT_INTERVAL", "1800"))
# 데이터셋 전체 상한(MB). 넘으면 수집을 멈춘다 — 조용히 디스크를 채우고
# 서비스가 함께 죽는 것이 가장 나쁘다.
MAX_TOTAL_MB = float(os.environ.get("URBANGUARD_ROAD_COLLECT_MAX_MB", "2048"))

# 저장을 건너뛴 이유
SKIP_DISABLED = "disabled"
SKIP_INTERVAL = "interval"
SKIP_DAILY = "daily_limit"
SKIP_DISK = "disk_limit"
SKIP_MASK = "mask_failed"
SKIP_NO_FRAME = "no_frame"
SKIP_ERROR = "error"

_lock = threading.Lock()
_last_saved: dict[str, float] = {}       # camera_id -> epoch
_saved_today: dict[str, tuple[str, int]] = {}   # camera_id -> (YYYYMMDD, count)
_stats = {"saved": 0, "skipped": 0, "last_reason": "", "last_at": None}


# --- 설정 -------------------------------------------------------------------

def is_enabled() -> bool:
    """수집이 켜져 있는가. DB 설정이 없으면 **꺼짐**으로 본다."""
    try:
        from ..core import settings as S
        return S.get(S.KEY_ROAD_COLLECT).strip().lower() in ("on", "true", "1")
    except Exception:  # noqa: BLE001
        return False


# --- 집계 -------------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def dataset_size_mb() -> float:
    try:
        total = sum(p.stat().st_size for p in DATASET_DIR.rglob("*.jpg"))
        return total / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0.0


def frame_count() -> int:
    try:
        return sum(1 for _ in DATASET_DIR.rglob("*.jpg"))
    except Exception:  # noqa: BLE001
        return 0


def status() -> dict:
    """화면에 보여 줄 수집 현황."""
    with _lock:
        st = dict(_stats)
    return {
        **st,
        "enabled": is_enabled(),
        "frames": frame_count(),
        "size_mb": round(dataset_size_mb(), 1),
        "max_mb": MAX_TOTAL_MB,
        "per_day": MAX_PER_DAY,
        "min_interval_sec": MIN_INTERVAL_SEC,
        "dir": str(DATASET_DIR),
    }


def reset_state() -> None:
    """테스트용 — 누적 카운터를 지운다."""
    with _lock:
        _last_saved.clear()
        _saved_today.clear()
        _stats.update(saved=0, skipped=0, last_reason="", last_at=None)


def _note(saved: int, reason: str) -> None:
    with _lock:
        if saved:
            _stats["saved"] += saved
        else:
            _stats["skipped"] += 1
        _stats["last_reason"] = reason
        _stats["last_at"] = datetime.now(timezone.utc).isoformat()


# --- 저장 -------------------------------------------------------------------

def _allowed(camera_id: str) -> str:
    """지금 이 지점을 담아도 되는가. 되면 빈 문자열, 아니면 사유."""
    now = time.time()
    with _lock:
        last = _last_saved.get(camera_id, 0.0)
        if now - last < MIN_INTERVAL_SEC:
            return SKIP_INTERVAL
        day, cnt = _saved_today.get(camera_id, ("", 0))
        if day == _today() and cnt >= MAX_PER_DAY:
            return SKIP_DAILY
    if dataset_size_mb() >= MAX_TOTAL_MB:
        return SKIP_DISK
    return ""


def _register(camera_id: str, n: int) -> None:
    with _lock:
        _last_saved[camera_id] = time.time()
        day, cnt = _saved_today.get(camera_id, ("", 0))
        _saved_today[camera_id] = (
            _today(), (cnt + n) if day == _today() else n)


def collect(camera_id: str, camera_name: str, frames: list, *,
            source: str = "continuous") -> dict:
    """관측 프레임 중 몇 장을 학습용으로 남긴다.

    ``frames`` 는 ``analyze_cctv`` 가 뽑은 ``(index, ndarray)`` 목록이다.
    호출자를 막지 않도록 어떤 실패도 예외로 올리지 않는다 — 데이터 수집
    때문에 관제 분석이 멈추면 본말이 전도된다.
    """
    if not is_enabled():
        return {"saved": 0, "reason": SKIP_DISABLED}
    if not camera_id or not frames:
        _note(0, SKIP_NO_FRAME)
        return {"saved": 0, "reason": SKIP_NO_FRAME}

    reason = _allowed(camera_id)
    if reason:
        _note(0, reason)
        return {"saved": 0, "reason": reason}

    try:
        return _save(camera_id, camera_name, frames, source)
    except Exception:  # noqa: BLE001
        log.exception("노면 학습 프레임 저장 실패 camera=%s", camera_id)
        _note(0, SKIP_ERROR)
        return {"saved": 0, "reason": SKIP_ERROR}


def _pick(frames: list) -> list:
    """관측 창에서 고르게 뽑는다. 연속한 프레임은 서로 거의 같다."""
    n = min(MAX_PER_ROUND, len(frames))
    if n <= 0:
        return []
    if n == 1:
        return [frames[len(frames) // 2][1]]
    step = (len(frames) - 1) / (n - 1)
    return [frames[round(i * step)][1] for i in range(n)]


def _save(camera_id: str, camera_name: str, frames: list, source: str) -> dict:
    import cv2

    from ..core import image_mask

    out_dir = DATASET_DIR / camera_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved, records = 0, []

    for i, frame in enumerate(_pick(frames)):
        img = frame.copy()
        status_, masked_n = image_mask.mask_array(img)
        # ⚠️ 가리지 못한 프레임은 남기지 않는다. 오래 보관하는 파일이므로
        # 「나중에 확인」이 통하지 않는다.
        if status_ in image_mask.NEEDS_REVIEW:
            log.warning("마스킹하지 못해 학습 프레임을 버립니다 camera=%s (%s)",
                        camera_id, image_mask.STATUS_LABELS.get(status_, status_))
            continue
        name = f"{stamp}_{i}.jpg" if i else f"{stamp}.jpg"
        path = out_dir / name
        # ⚠️ ``cv2.imwrite`` 를 쓰지 않는다 — OpenCV 는 Windows 에서 **한글이
        # 섞인 경로에 쓰지 못하고 조용히 False 를 돌려준다.** 설치 경로에
        # 기관명·사용자명이 한글로 들어가는 일이 흔해 그대로 두면 수집이
        # 통째로 멈춘 채 로그만 쌓인다. 인코딩과 쓰기를 분리하면 무관해진다.
        ok, buf = cv2.imencode(".jpg", img)
        if not ok:
            log.warning("학습 프레임을 인코딩하지 못했습니다: %s", path)
            continue
        try:
            path.write_bytes(buf.tobytes())
        except OSError:
            log.exception("학습 프레임을 쓰지 못했습니다: %s", path)
            continue
        h, w = img.shape[:2]
        records.append({
            "file": name, "camera_id": camera_id, "camera_name": camera_name,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source": source, "width": w, "height": h,
            "mask_status": status_, "masked_people": masked_n,
            # 번호판은 가리지 못한다. 이 사실을 파일 옆에 남겨야 나중에
            # 외부로 내보낼 때 확인할 수 있다.
            "plate_masked": False,
        })
        saved += 1

    if not saved:
        _note(0, SKIP_MASK)
        return {"saved": 0, "reason": SKIP_MASK}

    # 출처·마스킹 상태를 파일 옆에 남긴다. 이미지만 쌓아 두면 몇 달 뒤에
    # 「이건 언제 어디서 받은 것이고 가려진 것인가」를 알 수 없다.
    try:
        with (out_dir / META_NAME).open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.exception("수집 메타데이터를 쓰지 못했습니다 camera=%s", camera_id)

    _register(camera_id, saved)
    _note(saved, "ok")
    log.info("노면 학습 프레임 %d장 저장 — %s(%s)", saved, camera_name, camera_id)
    return {"saved": saved, "reason": "ok", "dir": str(out_dir)}


def sink(camera_id: str, camera_name: str, *, source: str = "continuous"):
    """``analyze_cctv(frame_sink=...)`` 에 넘길 콜백을 만든다."""
    def _sink(frames: list) -> None:
        collect(camera_id, camera_name, frames, source=source)
    return _sink


# --- 사용자 영상에서 프레임 뽑기 ---------------------------------------------
# CCTV 자동 수집만으로는 한계가 있다. 등록된 부산 교통 CCTV는 대부분
# 320~352×240 이라 사람이 손상 박스를 그을 수 없고(적합도 평가 결과 8곳 중
# 1곳만 「조건부」), 그 화면에서 아무리 많이 모아도 라벨을 못 단다.
#
# 반면 담당자가 현장에서 찍은 영상이나 차량 블랙박스 영상은 노면이 크게
# 잡힌다. **그런 자료가 있으면 그것부터 쓰는 것이 맞다.** 여기가 그 통로다.
#
# 자동 수집의 상한(지점당 하루·최소 간격)은 적용하지 않는다 — 사람이 파일을
# 골라 올리는 명시적 행위라 저절로 쌓이지 않는다. 디스크 총량 상한만 지킨다.

# 한 영상에서 뽑을 최대 장수. 같은 영상에서 수백 장을 뽑아도 장면이 비슷해
# 라벨링 비용만 늘고 학습에는 보탬이 되지 않는다.
UPLOAD_MAX_FRAMES = int(os.environ.get("URBANGUARD_ROAD_UPLOAD_MAX", "60"))
# 기본 추출 간격(초). 주행 영상 기준으로 2초면 장면이 충분히 바뀐다.
UPLOAD_INTERVAL_SEC = float(os.environ.get("URBANGUARD_ROAD_UPLOAD_INTERVAL", "2"))


def _upload_dir_id() -> str:
    """업로드 1회 = 폴더 1개.

    폴더명을 ASCII 로만 만드는 이유 — 라벨링 도구
    (``scripts/label_road_defects.py``)가 ``cv2.imread`` 를 쓰는데, OpenCV 는
    Windows 에서 한글이 섞인 경로를 읽지 못하고 조용히 ``None`` 을 돌려준다.
    사람이 붙인 이름은 ``meta.jsonl`` 에 남긴다.
    """
    return "UPLOAD-" + time.strftime("%Y%m%d_%H%M%S")


def _seek_works(cap, step: int) -> bool:
    """이 영상에서 프레임 탐색이 실제로 먹히는가.

    코덱·컨테이너에 따라 ``CAP_PROP_POS_FRAMES`` 설정이 조용히 무시된다.
    그대로 믿고 뛰면 같은 프레임을 반복해서 뽑거나 무한 루프에 빠지므로,
    **한 번 시험해 보고** 위치가 실제로 움직였을 때만 쓴다.
    """
    import cv2

    try:
        if not cap.set(cv2.CAP_PROP_POS_FRAMES, step):
            return False
        moved = cap.get(cv2.CAP_PROP_POS_FRAMES)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        # 키프레임 정렬 때문에 정확히 맞지는 않는다. 「움직였는가」만 본다.
        return moved >= step * 0.5
    except Exception:  # noqa: BLE001
        return False


def ingest_video(video_path, *, label: str = "",
                 interval_sec: float | None = None,
                 max_frames: int | None = None,
                 source: str = "upload") -> dict:
    """영상 파일에서 프레임을 고르게 뽑아 학습 데이터로 남긴다.

    ⚠️ **원본 영상은 남기지 않는다.** 마스킹하지 않은 도로 영상을 디스크에
    쌓아 두는 것이 바로 개인정보 사전검토서가 지적한 문제다. 프레임만 가려서
    저장하고 원본은 호출자가 지운다.
    """
    import cv2

    from ..core import image_mask

    interval = max(interval_sec or UPLOAD_INTERVAL_SEC, 0.1)
    cap_frames = max(min(max_frames or UPLOAD_MAX_FRAMES, 500), 1)

    if dataset_size_mb() >= MAX_TOTAL_MB:
        return {"ok": False, "saved": 0, "reason": SKIP_DISK,
                "error": (f"데이터셋이 상한({MAX_TOTAL_MB:.0f}MB)에 닿았습니다. "
                          "기존 프레임을 정리한 뒤 다시 시도해 주세요.")}

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        return {"ok": False, "saved": 0, "reason": SKIP_ERROR,
                "error": "영상을 열지 못했습니다. 손상되었거나 지원하지 않는 코덱입니다."}

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    # fps 를 못 읽는 컨테이너가 있어 30으로 가정한다. 간격이 조금 어긋나도
    # 표본 추출 목적에는 지장이 없다.
    step = max(int(round((fps or 30.0) * interval)), 1)
    if total > 0:
        # 긴 영상이면 간격을 넓혀 **영상 전체에 고르게** 퍼뜨린다. 앞부분
        # 60장만 뽑으면 촬영 초반 몇 분만 학습하게 된다.
        step = max(step, -(-total // cap_frames))

    dir_id = _upload_dir_id()
    out_dir = DATASET_DIR / dir_id
    saved = 0
    mask_skipped = 0
    records: list[dict] = []
    idx = 0
    # 표본 간격이 넓으면 건너뛰는 프레임 수가 어마어마해진다(10분 영상에서
    # 60장 = 프레임 1만 8천 개를 지나쳐야 한다). 탐색이 되는 컨테이너면
    # 곧바로 뛰어넘고, 안 되면 ``grab()`` 으로 훑는다 — ``grab()`` 은 디코딩을
    # 하지 않아 ``read()`` 보다 훨씬 싸다.
    can_seek = _seek_works(cap, step) if step > 1 else False
    try:
        while saved < cap_frames:
            if idx % step:
                if can_seek:
                    idx += step - (idx % step)
                    if not cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
                        can_seek = False
                    continue
                if not cap.grab():
                    break
                idx += 1
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            pos_sec = (idx / fps) if fps else None
            idx += 1

            img = frame.copy()
            status_, masked_n = image_mask.mask_array(img)
            # 자동 수집과 같은 규칙 — 가리지 못한 프레임은 남기지 않는다.
            if status_ in image_mask.NEEDS_REVIEW:
                mask_skipped += 1
                continue
            enc_ok, buf = cv2.imencode(".jpg", img)
            if not enc_ok:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            name = f"{saved:04d}.jpg"
            try:
                (out_dir / name).write_bytes(buf.tobytes())
            except OSError:
                log.exception("업로드 프레임을 쓰지 못했습니다: %s", out_dir / name)
                continue
            h, w = img.shape[:2]
            records.append({
                "file": name, "camera_id": dir_id,
                "camera_name": label or "업로드 영상",
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "source": source, "width": w, "height": h,
                "mask_status": status_, "masked_people": masked_n,
                "plate_masked": False,
                "video_label": label,
                "video_position_sec": (round(pos_sec, 2)
                                       if pos_sec is not None else None),
            })
            saved += 1
            if saved % 10 == 0 and dataset_size_mb() >= MAX_TOTAL_MB:
                log.warning("데이터셋 상한에 닿아 추출을 중단합니다")
                break
    finally:
        cap.release()

    if records:
        try:
            with (out_dir / META_NAME).open("a", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("업로드 메타데이터를 쓰지 못했습니다 dir=%s", dir_id)

    if not saved:
        why = ("사람을 가리지 못해 전부 제외했습니다(마스킹 모델 확인 필요)."
               if mask_skipped else "영상에서 프레임을 얻지 못했습니다.")
        return {"ok": False, "saved": 0, "reason": SKIP_MASK if mask_skipped
                else SKIP_NO_FRAME, "error": why, "mask_skipped": mask_skipped}

    _note(saved, "upload")
    log.info("업로드 영상에서 학습 프레임 %d장 저장 — %s (%s)",
             saved, label or "이름 없음", dir_id)
    duration = (total / fps) if (fps and total) else None
    return {"ok": True, "saved": saved, "mask_skipped": mask_skipped,
            "dir": str(out_dir), "dir_id": dir_id,
            "duration_sec": round(duration, 1) if duration else None,
            "interval_sec": round(step / fps, 2) if fps else None,
            "resolution": f"{records[0]['width']}×{records[0]['height']}"}
