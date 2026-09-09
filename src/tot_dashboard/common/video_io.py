"""Generic video/image/folder source I/O helpers.

Ported from underpath_flood_dashboard's ``src/video_processor.py`` — only the
source-agnostic probing/sampling functions, none of which are flood-specific.
The ``Pipeline``/``FrameResult`` orchestration classes from that file are
domain-specific and live in ``flood/standalone_pipeline.py`` instead (Phase 2).
"""
from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import cv2
import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}


def imread_unicode(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """``cv2.imread`` 의 비ASCII 경로 대응 대체.

    2026-08-25 발견 — Windows에서 ``cv2.imread``는 경로를 시스템 코드페이지로
    인코딩해 ``fopen``을 부른다. **파일명뿐 아니라 상위 폴더명 어디에든** 한글
    등 비ASCII 문자가 있으면 예외 없이 조용히 ``None``을 반환한다(학습 데이터
    보관소 ``D:\\dev-PoC_DATA\\02_학습데이터_침수\\...``의 폴더명 자체가
    한글이라 AI 모델 학습 화면 실사용 중 실제로 걸렸다 — 전 이미지가
    「can't open/read file」로 실패). ``extract_flood_frames.py``가 저장(write)
    쪽에 적용한 것과 같은 해법을 읽기(read) 쪽에 적용한다 — 파일을 바이트로
    읽어(``np.fromfile``은 유니코드 경로를 그대로 다룬다) ``cv2.imdecode``로
    디코딩한다.
    """
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path: str | Path, img) -> bool:
    """``cv2.imwrite`` 의 비ASCII 경로 대응 대체.

    Windows에서 ``cv2.imwrite``는 경로에 비ASCII(한글 등)가 들어가면
    **예외 없이 False만 반환하고 조용히 실패**한다 — 실제로 한글 파일명
    영상("충청북도 옥천군_도로 침수 영상_...")에서 "214장 저장" 로그가
    찍혔는데 실제 저장은 0장이었던 사고가 있었다(``extract_flood_frames.py``
    에서 처음 발견). ``imencode`` + ``tofile``(``np.fromfile``과 대칭되는
    유니코드 경로 API)로 우회한다.

    2026-08-26 — ``extract_flood_frames.py``에 있던 것을 세 번째 사본
    (``extract_traffic_incident_frames.py``)이 생기는 시점에 여기로 옮겼다.
    """
    ok, buf = cv2.imencode(Path(path).suffix or ".jpg", img)
    if not ok:
        return False
    buf.tofile(str(path))
    return True


def ascii_stem(name: str) -> str:
    """파일명을 ASCII로 정규화 — ``imwrite_unicode`` 를 안 쓰는 경로에서도
    저장 파일명 자체는 안전하게 만들어야 할 때 쓴다.

    연속 언더스코어는 하나로 정리한다. 결과가 비면 ``"video"`` 로 대체한다.
    """
    safe = "".join(c if (c.isascii() and (c.isalnum() or c in "-_")) else "_" for c in name)
    safe = "_".join(filter(None, safe.split("_")))
    return safe or "video"


_cv2_imread_patched = False


def patch_cv2_imread_for_unicode_paths() -> None:
    """``cv2.imread``를 프로세스 전체에서 :func:`imread_unicode`로 바꾼다.

    학습 스크립트가 직접 부르는 곳뿐 아니라, **ultralytics 같은 외부
    라이브러리의 내부 데이터로더도 같은 문제를 겪는다** — 그쪽 코드는 고칠 수
    없으므로, ``cv2`` 모듈 자체를 패치해 학습 스크립트가 시작되는 시점에
    한 번만 적용한다. 학습 스크립트(도로·교통위험처럼 ultralytics를 쓰는
    쪽 포함) 맨 앞에서 다른 모듈을 임포트하기 전에 호출해야 한다.
    """
    global _cv2_imread_patched
    if _cv2_imread_patched:
        return
    cv2.imread = imread_unicode  # type: ignore[assignment]
    _cv2_imread_patched = True


@dataclass
class VideoInfo:
    fps: float
    frame_count: int
    duration_sec: float
    width: int
    height: int


def probe_video(path: str | Path) -> VideoInfo:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise IOError(f"Cannot open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if fps <= 0:
        fps = 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    duration = frame_count / fps if fps else 0.0
    return VideoInfo(fps, frame_count, duration, width, height)


def read_first_frame(path: str | Path) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(path))
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def list_folder_images(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def detect_letterbox(frame: np.ndarray, dark_thresh: int = 18,
                     row_frac: float = 0.98) -> tuple[int, int, int, int]:
    """검은 여백(레터박스/필러박스) 경계 (top, bottom, left, right).

    각 행/열의 ``row_frac`` 이상 픽셀이 ``dark_thresh`` 미만이면 여백으로 본다.
    여백이 없으면 (0, h, 0, w)를 그대로 돌려준다.

    ⚠️ 이 처리는 **학습·추론 양쪽에 반드시 적용**해야 한다. 물 세그멘테이션
    모델은 레터박스 영상을 학습한 적이 없어, 검은 띠를 물로 오인한다(실측:
    기존 Ultralytics 모델 19%, 신규 모델 18.9% 오탐). 여백을 잘라내면 사라진다.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    dark = gray < dark_thresh
    row_dark = dark.mean(axis=1) >= row_frac
    col_dark = dark.mean(axis=0) >= row_frac

    top = 0
    while top < h and row_dark[top]:
        top += 1
    bottom = h
    while bottom > top and row_dark[bottom - 1]:
        bottom -= 1
    left = 0
    while left < w and col_dark[left]:
        left += 1
    right = w
    while right > left and col_dark[right - 1]:
        right -= 1

    # 이상치 방어: 잘라낸 결과가 원본의 절반 미만이면 크롭하지 않음
    if (bottom - top) < h * 0.5 or (right - left) < w * 0.5:
        return 0, h, 0, w
    return top, bottom, left, right


def crop_letterbox(frame: np.ndarray) -> np.ndarray:
    """레터박스를 감지해 잘라낸 프레임(없으면 원본 그대로)."""
    t, b, l, r = detect_letterbox(frame)
    return frame[t:b, l:r]


def iter_source(source: dict[str, Any], process_every_seconds: float = 1.0
                ) -> Iterator[tuple[int, float, np.ndarray]]:
    """Yield ``(frame_number, timestamp_sec, bgr_frame)`` for any source type.

    source = {"type": "video"|"image"|"folder", "path": ..., "paths": [...]}
    Videos are sampled every ``round(fps * process_every_seconds)`` frames.
    Image folders advance the pseudo-timestamp by ``process_every_seconds``.
    """
    kind = source["type"]
    if kind == "video":
        cap = cv2.VideoCapture(str(source["path"]))
        if not cap.isOpened():
            raise IOError(f"Cannot open video: {source['path']}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if fps <= 0:
            fps = 30.0
        stride = max(1, round(fps * process_every_seconds))
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % stride == 0:
                yield idx, idx / fps, frame
            idx += 1
        cap.release()
    elif kind == "image":
        frame = cv2.imread(str(source["path"]))
        if frame is None:
            raise IOError(f"Cannot read image: {source['path']}")
        yield 0, 0.0, frame
    elif kind == "folder":
        paths = source.get("paths") or list_folder_images(source["path"])
        for i, p in enumerate(paths):
            frame = cv2.imread(str(p))
            if frame is None:
                continue
            yield i, float(i) * process_every_seconds, frame
    else:
        raise ValueError(f"Unknown source type: {kind}")


def get_preview_frame(source: dict[str, Any], pos: float = 0.0) -> np.ndarray | None:
    """Grab one frame for preview/single-frame analysis.

    ``pos`` is seconds for a video, or an integer image index for a folder.
    """
    kind = source["type"]
    if kind == "video":
        cap = cv2.VideoCapture(str(source["path"]))
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        target = int(max(0.0, pos) * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = cap.read()
        cap.release()
        return frame if ok else read_first_frame(source["path"])
    if kind == "image":
        return cv2.imread(str(source["path"]))
    if kind == "folder":
        paths = source.get("paths") or [str(p) for p in list_folder_images(source["path"])]
        if not paths:
            return None
        i = int(max(0, min(len(paths) - 1, int(pos))))
        return cv2.imread(str(paths[i]))
    return None


def source_name(source: dict[str, Any]) -> str:
    if source["type"] == "folder":
        return Path(source["path"]).name + "/"
    return Path(source.get("path", "source")).name


@contextlib.contextmanager
def suppress_native_stderr():
    """Silence OpenCV/FFMPEG C-level stderr (e.g. OpenH264 codec-probe spam).

    Those messages are printed from native code, so redirecting Python's
    ``sys.stderr`` is not enough — we duplicate the OS-level fd 2. Falls back
    to a no-op if stderr has no real file descriptor. Ported from
    underpath_flood_dashboard's ``src/archive_manager.py``; SAM's
    ``scripts/build_case.py`` needs the same H.264-transcode-probe suppression.
    """
    try:
        stderr_fd = sys.stderr.fileno()
    except Exception:
        yield
        return
    saved = os.dup(stderr_fd)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, stderr_fd)
        yield
    finally:
        os.dup2(saved, stderr_fd)
        os.close(devnull)
        os.close(saved)


def open_h264_video_writer(path: str | Path, fps: float, size: tuple[int, int]) -> cv2.VideoWriter:
    """Open a cv2.VideoWriter, preferring H.264 (avc1) so the clip plays
    in-browser; falls back to mp4v if the H.264 encoder isn't available in this
    OpenCV build. Wrapped in :func:`suppress_native_stderr` to hide FFMPEG's
    native OpenH264 error spam on machines whose openh264 DLL is missing.
    """
    w, h = size
    with suppress_native_stderr():
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"avc1"), fps, (w, h))
        if not writer.isOpened():
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    return writer
