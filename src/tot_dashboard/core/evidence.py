"""탐지 이벤트 증거 영상 — 링 버퍼와 클립 저장 (S-88).

**왜 링 버퍼인가.** 이벤트는 상황이 이미 벌어진 **뒤에** 탐지된다. 탐지된
시점부터 녹화를 시작하면 정작 중요한 「어떻게 그렇게 됐는가」가 빠진다.
그래서 카메라마다 최근 화면을 **미리 담아 두었다가**, 이벤트가 뜨면 그
직전 구간과 이후 구간을 합쳐 한 편으로 남긴다.

**⚠️ 이 모듈이 디스크에 쌓는 것은 개인정보다.** 관제 영상에는 사람과 차량이
찍힌다. 그래서

* 어느 **등급부터** 남길지 관리자가 정한다(:data:`~.settings.KEY_EVIDENCE_LEVELS`)
* 남은 자료는 화면에서 **확인하고 지울 수 있다**(S-88)
* 내려받기와 삭제는 **감사 로그에 남는다**
* 밖으로 내주면 **영상 반출 관리대장(S-94)** 에 적어야 한다

**형식.** 정지영상(JPEG)은 항상 남기고, 클립(webm)은 되면 남긴다.
정지영상은 어떤 환경에서도 실패하지 않지만 클립은 코덱에 기댄다 — 증거가
아예 없는 것보다 한 장이라도 있는 편이 낫다.

★ **2026-08-20 코덱을 VP9(webm)로 바꿨다.** 예전에는 ``mp4v``(MPEG-4 Part 2)
였는데, **이 환경에서 실제로 브라우저(Chrome)에 열어 재생을 확인**해 보니
``MEDIA_ERR_SRC_NOT_SUPPORTED`` 로 재생되지 않았다 — 팝업에서 영상을 확인하려
해도 못 봤다는 뜻이다. 이 환경에서 실제로 열리는 코덱을 다시 실측했다.

| FourCC | 이 환경에서 인코딩 | 브라우저 재생 |
|---|---|---|
| avc1/h264/H264/X264 | ✗ (libopenh264 인코더 없음) | — |
| mp4v (예전 값) | ✔ | ✗ |
| **vp09 (지금 값)** | **✔** | **✔** |

⚠️ H.264(avc1) 는 이 환경에 **인코더 라이브러리(libopenh264)가 없어** 지금도
안 열린다. VP9 는 **웹 표준 코덱이라 인코더가 FFmpeg 에 내장**돼 있어 별도
설치 없이 된다 — 그래서 컨테이너도 mp4 가 아니라 **webm** 으로 바꿨다
(VP9 는 mp4 컨테이너 안에서는 브라우저 지원이 들쭉날쭉하다).
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common.config import PROJECT_ROOT

log = logging.getLogger("urbanguard.evidence")

# 저장 위치. **환경변수로 옮길 수 있어야 한다** — 영상은 용량 때문에 별도
# 볼륨으로 빼는 일이 실제로 있고, 시험은 운영 데이터 폴더를 건드리면 안 된다.
_dir_env = os.environ.get("URBANGUARD_EVIDENCE_DIR", "").strip()
EVIDENCE_DIR = Path(_dir_env) if _dir_env else PROJECT_ROOT / "data" / "evidence"
# DB에는 프로젝트 기준 상대경로로 남긴다 — 서버를 옮겨도 경로가 안 깨진다
# (:mod:`.video_store` 와 같은 규칙).
REL_PREFIX = "data/evidence"

# 클립 앞뒤 길이. 앞 구간이 링 버퍼에서 나온다.
PRE_SEC = 10
POST_SEC = 10

# 버퍼·클립 프레임률. **파이프라인 속도(5fps)보다 낮게 잡는다.**
# 39개 지점을 5fps로 담으면 메모리가 수백 MB로 뛴다. 증거는 「무슨 일이
# 있었는가」를 보여 주면 되므로 2fps로 충분하다.
FPS = 2

# 카메라당 링 버퍼 상한. 초 기준으로 자르되 **바이트로도 막는다** — 해상도가
# 큰 지점 하나가 메모리를 독차지하는 것을 방지한다.
RING_MAX_FRAMES = PRE_SEC * FPS
RING_MAX_BYTES = 3 * 1024 * 1024

# 동시에 수집 중인 이벤트 수 상한. 폭우로 전 지점이 동시에 터질 때
# 메모리가 무너지지 않게 한다. 넘치면 그 이벤트는 정지영상만 남는다.
MAX_ACTIVE_JOBS = 8

JPEG_QUALITY = 80

KIND_IMAGE = "image"
KIND_CLIP = "clip"
KIND_LABELS = {KIND_IMAGE: "정지영상", KIND_CLIP: "영상 클립"}

_lock = threading.Lock()
_rings: dict[str, deque] = {}
_jobs: dict[int, "_Job"] = {}       # event_id -> 수집 중인 작업
_last_push: dict[str, float] = {}   # camera_id -> 마지막으로 담은 시각


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Job:
    """이벤트 하나의 증거 수집 상태."""

    def __init__(self, event_id: int, camera_id: str, level: str,
                 pre: list[tuple[float, bytes]], *,
                 domain: str = "",
                 boxes: list[dict] | None = None,
                 frame_w: int = 0, frame_h: int = 0):
        self.event_id = event_id
        self.camera_id = camera_id
        self.level = level
        # ⚠️ **예전에는 클립 행의 domain 이 늘 빈 문자열이었다**(2026-08-20
        #   발견). 정지영상만 이벤트에서 domain 을 받고, 클립은 Job 에 그
        #   값을 들고 있지 않아 `_finish_clips` 가 `""` 를 그대로 썼다.
        #   그 결과 **도메인 필터·배지가 클립에는 전혀 안 먹혔다.**
        self.domain = domain
        self.frames: list[tuple[float, bytes]] = list(pre)
        self.deadline = time.monotonic() + POST_SEC
        self.started = _now()
        # ★ **트리거 순간**의 상자를 그대로 들고 있는다. 클립은 뒤 구간이
        #   끝나야 파일로 굳는데, 그때 다시 "최신 상자"를 물으면 이미 다른
        #   틱의 값이라 트리거 순간과 어긋난다.
        self.boxes = list(boxes or [])
        self.frame_w = frame_w
        self.frame_h = frame_h


# --- 프레임 수집 -------------------------------------------------------------

def push(camera_id: str, frame, *, quality: int = JPEG_QUALITY) -> None:
    """파이프라인이 매 프레임 부른다. **절대 예외를 밖으로 내지 않는다** —
    증거 수집 때문에 탐지가 멈추면 본말이 전도된다.

    ``FPS`` 보다 자주 불려도 알아서 솎아 낸다. 파이프라인마다 속도가 달라
    호출 쪽에서 맞추게 하면 반드시 어긋난다.
    """
    try:
        now = time.monotonic()
        with _lock:
            last = _last_push.get(camera_id, 0.0)
            if now - last < (1.0 / FPS):
                return
            _last_push[camera_id] = now
            active = [j for j in _jobs.values() if j.camera_id == camera_id]
            need_ring = True

        blob = _encode(frame, quality)
        if blob is None:
            return

        with _lock:
            if need_ring:
                ring = _rings.setdefault(camera_id, deque())
                ring.append((now, blob))
                _trim(ring)
            for job in active:
                # 상한을 넘으면 더 담지 않는다 — 파일이 커지는 것보다
                # 메모리가 터지는 것이 훨씬 나쁘다.
                if len(job.frames) < (PRE_SEC + POST_SEC) * FPS + 4:
                    job.frames.append((now, blob))
    except Exception as e:  # noqa: BLE001
        log.debug("증거 프레임 수집 실패 %s: %s", camera_id, str(e)[:120])


def _trim(ring: deque) -> None:
    while len(ring) > RING_MAX_FRAMES:
        ring.popleft()
    total = sum(len(b) for _, b in ring)
    while ring and total > RING_MAX_BYTES:
        _, b = ring.popleft()
        total -= len(b)


def _encode(frame, quality: int) -> bytes | None:
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        return buf.tobytes() if ok else None
    except Exception:  # noqa: BLE001
        return None


def buffered(camera_id: str) -> int:
    with _lock:
        return len(_rings.get(camera_id, ()))


def reset() -> None:
    """시험용. 프로세스 상태를 비운다."""
    with _lock:
        _rings.clear()
        _jobs.clear()
        _last_push.clear()
        _last_boxes.clear()


# --- 이벤트가 난 위치 (탐지 상자) --------------------------------------------
#
# ★ **지어낼 수 없는 것은 저장하지 않는다.**
#
#   이 캐시는 프레임처럼 「최근 값」만 들고 있는다. 파이프라인이 매 틱
#   실제로 관측한 값(물 마스크 경계·추적 상자·손상 상자)만 넣는다 — 여기에
#   아무것도 넣지 않은 지점은 그냥 비어 있고, 팝업은 「없다」고 정직하게
#   말한다(routes_evidence.py 의 ``has_box`` 참고).
_last_boxes: dict[str, tuple[list[dict], int, int]] = {}


def push_boxes(camera_id: str, boxes: list[dict], frame_wh: tuple[int, int]) -> None:
    """이 지점의 **최신 탐지 상자**를 기억해 둔다.

    파이프라인이 그 틱에 실제로 찾은 것만 넘긴다 — 없으면 빈 리스트를
    넘겨서 **낡은 상자가 남아 다음 이벤트에 잘못 붙지 않게** 한다.

    ``push()`` 와 짝을 이루지만 프레임 자체가 아니라 **그 프레임에 대한
    판정 결과**라서 따로 둔다. 도메인마다 한 틱 안에서 프레임을 담은 뒤
    (또는 담기 전) 판정이 나오는 시점에 한 번 부르면 된다.

    절대 예외를 밖으로 내지 않는다 — 증거 수집이 탐지를 막으면 안 된다.
    """
    try:
        clean = []
        for b in (boxes or []):
            clean.append({"x1": float(b["x1"]), "y1": float(b["y1"]),
                          "x2": float(b["x2"]), "y2": float(b["y2"]),
                          "label": str(b.get("label", ""))[:64]})
        with _lock:
            _last_boxes[camera_id] = (clean, int(frame_wh[0]), int(frame_wh[1]))
    except Exception as e:  # noqa: BLE001
        log.debug("증거 상자 기록 실패 %s: %s", camera_id, str(e)[:120])


def latest_boxes(camera_id: str) -> tuple[list[dict], int, int] | None:
    """이 지점의 최신 상자. 한 번도 없었으면 ``None``.

    ``None`` 과 ``([], w, h)`` 를 구분한다 — 전자는 「이 도메인은 상자를
    아예 안 준다」, 후자는 「이번 틱엔 없었다」다. 지금은 화면에서 둘 다
    「없다」로 보이지만, 나중에 원인을 가릴 때 이 구분이 필요하다.
    """
    with _lock:
        return _last_boxes.get(camera_id)


# --- 수집 시작·종료 ----------------------------------------------------------

def start(event_id: int, camera_id: str, level: str, *,
         domain: str = "",
         boxes: list[dict] | None = None,
         frame_w: int = 0, frame_h: int = 0) -> bool:
    """이벤트에 대한 증거 수집을 시작한다. 이미 수집 중이면 ``False``.

    **부르는 쪽이 등급을 걸러 준다**(:func:`~.settings.evidence_levels`).
    여기서는 「받으면 담는다」만 한다 — 정책과 기계를 섞지 않는다.

    ``boxes`` 는 **트리거 순간**의 값이다. 클립은 뒤 구간(10초)이 끝나야
    파일로 굳는데, 그때 다시 최신 상자를 물으면 이미 다른 틱의 값이라
    트리거 순간과 어긋난다 — 그래서 시작 시점에 받아 Job 에 담아 둔다.
    """
    with _lock:
        if event_id in _jobs:
            return False
        if len(_jobs) >= MAX_ACTIVE_JOBS:
            log.warning("증거 동시 수집 상한(%d) — 이벤트 #%s 는 정지영상만 남깁니다",
                        MAX_ACTIVE_JOBS, event_id)
            return False
        pre = list(_rings.get(camera_id, ()))
        _jobs[event_id] = _Job(event_id, camera_id, level, pre, domain=domain,
                               boxes=boxes, frame_w=frame_w, frame_h=frame_h)
    log.info("증거 수집 시작 event=%s cam=%s level=%s 앞구간=%d프레임",
             event_id, camera_id, level, len(pre))
    return True


def due() -> list[int]:
    """수집이 끝난 이벤트 ID. 워커가 주기적으로 확인한다."""
    now = time.monotonic()
    with _lock:
        return [eid for eid, j in _jobs.items() if now >= j.deadline]


def pop(event_id: int) -> "_Job | None":
    with _lock:
        return _jobs.pop(event_id, None)


def active_count() -> int:
    with _lock:
        return len(_jobs)


# --- 파일 쓰기 ---------------------------------------------------------------

def _paths(event_id: int, when: datetime) -> tuple[Path, str]:
    """``data/evidence/YYYYMM/`` 아래에 둔다.

    한 폴더에 수만 개가 쌓이면 탐색기도 백업도 느려진다. 달 단위로 나누면
    보존기간이 지난 자료를 폴더째 확인하기도 쉽다.
    """
    sub = when.strftime("%Y%m")
    d = EVIDENCE_DIR / sub
    d.mkdir(parents=True, exist_ok=True)
    return d, f"{REL_PREFIX}/{sub}"


def sha256_of(path: Path) -> str:
    """파일 해시. **증거 자료는 「바뀌지 않았음」을 말할 수 있어야 한다.**"""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def write_image(event_id: int, blob: bytes, when: datetime | None = None
                ) -> tuple[str, int, str] | None:
    """정지영상 저장 → (상대경로, 바이트수, sha256)."""
    when = when or _now()
    try:
        d, rel = _paths(event_id, when)
        name = f"ev{event_id}_{when.strftime('%Y%m%d_%H%M%S')}.jpg"
        path = d / name
        path.write_bytes(blob)
        return f"{rel}/{name}", path.stat().st_size, sha256_of(path)
    except Exception as e:  # noqa: BLE001
        log.warning("증거 정지영상 저장 실패 event=%s: %s", event_id, str(e)[:160])
        return None


def write_clip(event_id: int, frames: list[tuple[float, bytes]],
               when: datetime | None = None) -> tuple[str, int, str] | None:
    """클립 저장 → (상대경로, 바이트수, sha256). 실패하면 ``None``.

    **실패해도 조용히 넘긴다.** 정지영상은 이미 남아 있고, 클립이 없다고
    이벤트 처리가 막히면 안 된다. 다만 로그에는 남겨 원인을 볼 수 있게 한다.
    """
    if not frames:
        return None
    when = when or _now()
    try:
        import cv2
        import numpy as np

        first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8),
                             cv2.IMREAD_COLOR)
        if first is None:
            return None
        h, w = first.shape[:2]
        d, rel = _paths(event_id, when)
        name = f"ev{event_id}_{when.strftime('%Y%m%d_%H%M%S')}.webm"
        path = d / name

        # vp09/webm — 실제로 브라우저에서 재생을 확인하고 고른 코덱이다
        # (모듈 머리말 2026-08-20). H.264 는 이 환경에 인코더가 없다.
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"vp09"),
                                 float(FPS), (w, h))
        if not writer.isOpened():
            log.warning("증거 클립 인코더를 열지 못했습니다 event=%s", event_id)
            return None
        written = 0
        for _t, blob in frames:
            img = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h))
            writer.write(img)
            written += 1
        writer.release()
        if written == 0 or not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            return None
        return f"{rel}/{name}", path.stat().st_size, sha256_of(path)
    except Exception as e:  # noqa: BLE001
        log.warning("증거 클립 저장 실패 event=%s: %s", event_id, str(e)[:160])
        return None


def abs_path(rel: str) -> Path:
    """상대경로 → 실제 경로. **경로 조작을 막는다** — 파일명만 취하지 않고
    저장 폴더 밖으로 나가는지 확인한다.

    ⚠️ **저장 폴더(:data:`EVIDENCE_DIR`)를 기준으로 되짚는다.**
    ``PROJECT_ROOT`` 에 접두어를 다시 붙이는 방식으로 짜면, 저장 폴더를 다른
    디스크로 옮긴 순간 쓴 곳과 읽는 곳이 어긋나 「파일이 없습니다」가 된다.
    영상은 용량 때문에 별도 볼륨으로 빼는 일이 실제로 있다.
    """
    tail = (rel or "").replace("\\", "/").lstrip("/")
    prefix = REL_PREFIX + "/"
    if tail.startswith(prefix):
        tail = tail[len(prefix):]
    p = (EVIDENCE_DIR / tail).resolve()
    root = EVIDENCE_DIR.resolve()
    if not str(p).startswith(str(root)):
        raise ValueError(f"증거 저장 폴더 밖의 경로입니다: {rel}")
    return p


def remove_file(rel: str) -> bool:
    try:
        abs_path(rel).unlink(missing_ok=True)
        return True
    except (OSError, ValueError) as e:
        log.warning("증거 파일 삭제 실패 %s: %s", rel, str(e)[:120])
        return False


def latest_frame(camera_id: str) -> bytes | None:
    with _lock:
        ring = _rings.get(camera_id)
        return ring[-1][1] if ring else None


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def older_than(days: int) -> datetime:
    return _now() - timedelta(days=max(int(days), 0))


def months_ago(months: int) -> datetime:
    """``months`` 개월 전 시각.

    **30일 곱셈으로 때우지 않는다.** 「3개월 보존」이라고 해 놓고 실제로는
    90일에 지워지면, 달마다 기준이 달라져 규정과 어긋난다. 달력으로 센다.
    말일 보정도 한다(3월 31일의 1개월 전은 2월 28/29일).
    """
    n = max(int(months), 0)
    if n == 0:
        return _now()
    now = _now()
    year, month = now.year, now.month - n
    while month <= 0:
        month += 12
        year -= 1
    day = now.day
    while day > 28:
        try:
            return now.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return now.replace(year=year, month=month, day=day)
