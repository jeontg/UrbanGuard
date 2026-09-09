"""blocks.json 로딩 + 스트림 프레임 취득 공용 헬퍼.

``_load_blocks()`` / ``_grab_hls_frame()`` 은 원래
``service/main.py``, ``scripts/roi_editor.py``,
``scripts/collect_road_cctv_frames.py``, ``scripts/test_road_defect_detection.py``
에 각각 사본으로 존재했다. 침수 데이터 수집 스크립트를 추가하면서 5번째
사본이 생기게 되어 공용 모듈로 추출했다.

기존 호출부는 각자의 사본을 그대로 쓰고 있어도 동작에는 문제가 없으므로
이번에는 건드리지 않았다(신규 코드만 이 모듈을 사용). 정리 시점은 별도 판단.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np

from .config import PROJECT_ROOT


def blocks_path() -> Path:
    """blocks.json 경로. ``TOT_BLOCKS_PATH``로 재정의 가능(service/main.py와 동일 규약)."""
    override = os.environ.get("TOT_BLOCKS_PATH")
    return Path(override) if override else PROJECT_ROOT / "configs" / "blocks.json"


def load_blocks() -> list[dict]:
    return json.loads(blocks_path().read_text(encoding="utf-8"))["blocks"]


def find_block(block_id: str) -> dict | None:
    return next((b for b in load_blocks() if b["id"] == block_id), None)


def stream_url(block: dict) -> str | None:
    """HLS/RTSP 스트림 URL. 합성(synthetic) 블록 등은 None."""
    src = block.get("source") or {}
    if src.get("type") not in ("hls", "rtsp") or not src.get("url"):
        return None
    return src["url"]


def grab_stream_frame(url: str, timeout_tries: int = 30) -> np.ndarray | None:
    """라이브 스트림에서 첫 유효 프레임 1장. 실패 시 None.

    HLS는 연결 직후 몇 프레임이 비어 오는 경우가 있어 ``timeout_tries``회까지
    재시도한다.
    """
    cap = cv2.VideoCapture(url)
    frame = None
    if cap.isOpened():
        for _ in range(timeout_tries):
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
                break
        cap.release()
    return frame
