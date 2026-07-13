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
