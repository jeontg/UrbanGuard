"""분석용 동영상 업로드 저장.

지금까지는 서버에 파일을 미리 넣어 두고 경로를 손으로 적어야 했다. 지자체
담당자가 서버에 파일을 올릴 방법이 없으므로 실무에서 쓰이지 않는다.

⚠️ **영상은 개인정보가 담긴 자료다.** 업로드는 설정 권한자만 할 수 있고,
파일은 소스 트리가 아니라 데이터 폴더에 둔다(배포로 덮이지 않도록).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from ..common.config import PROJECT_ROOT

log = logging.getLogger("urbanguard.video_store")

VIDEO_DIR = PROJECT_ROOT / "data" / "videos"
# 저장 경로는 프로젝트 폴더 기준 상대경로로 DB에 남긴다 — 서버를 옮겨도
# 경로가 깨지지 않는다.
REL_PREFIX = "data/videos"

ALLOWED_SUFFIX = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
MAX_BYTES = 500 * 1024 * 1024        # 500MB
CHUNK = 1024 * 1024

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class VideoError(RuntimeError):
    """업로드 실패. 화면에 그대로 보여 줄 수 있는 문장을 담는다."""


def safe_name(camera_id: str, original: str) -> str:
    """저장 파일명. 한글 파일명이 서버 인코딩에 따라 깨지는 것을 막는다.

    한글은 ASCII로 옮길 수 없어 통째로 빠진다. 그래도 **카메라 ID가 앞에
    붙으므로** 폴더에서 어느 지점 영상인지 알아볼 수 있다.
    """
    suffix = Path(original or "").suffix.lower()
    stem = unicodedata.normalize("NFKD", Path(original or "").stem)
    stem = _SAFE.sub("_", stem).strip("_")[:40] or "video"
    cid = _SAFE.sub("_", camera_id or "CAM")
    return f"{cid}__{stem}{suffix}"


def _clear_previous(camera_id: str, keep: Path | None = None) -> None:
    """이 카메라의 이전 영상을 지운다.

    한 카메라는 영상 하나만 가리킨다(`source_path` 가 단일 값). 지우지 않으면
    바뀐 영상마다 파일이 쌓여 디스크만 먹고, 어느 것이 현재 것인지도 모른다.
    """
    cid = _SAFE.sub("_", camera_id or "CAM")
    for old in VIDEO_DIR.glob(f"{cid}__*"):
        if keep is not None and old == keep:
            continue
        try:
            old.unlink()
        except OSError:
            log.warning("이전 영상을 지우지 못했습니다: %s", old)


async def stage_upload(upload, camera_id: str) -> Path:
    """업로드 파일을 **임시 이름으로** 저장하고 그 경로를 돌려준다.

    바로 최종 위치에 쓰지 않는 이유 — 카메라 등록이 검증에서 걸리면 파일만
    남고 DB에는 아무것도 없다. 더 나쁘게는 **이전 영상을 지운 뒤 실패**해서
    멀쩡히 돌던 지점의 영상이 사라진다(실제로 겪었다).

    메모리에 통째로 올리지 않고 **조각으로 흘려 쓴다** — 수백 MB짜리 영상을
    한 번에 읽으면 서버가 그대로 죽는다.
    """
    filename = getattr(upload, "filename", "") or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise VideoError("동영상 파일만 올릴 수 있습니다 "
                         f"({', '.join(sorted(ALLOWED_SUFFIX))}).")

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEO_DIR / (safe_name(camera_id, filename) + ".uploading")

    total = 0
    try:
        with dest.open("wb") as f:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise VideoError(
                        f"파일이 너무 큽니다. {MAX_BYTES // 1024 // 1024}MB "
                        "이하로 올려 주세요.")
                f.write(chunk)
    except VideoError:
        dest.unlink(missing_ok=True)     # 잘린 파일을 남기지 않는다
        raise
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise VideoError(f"파일을 저장하지 못했습니다: {str(e)[:80]}") from e

    if total == 0:
        dest.unlink(missing_ok=True)
        raise VideoError("빈 파일입니다.")

    # 확장자만 믿지 않는다 — 실제로 열리는 영상인지 확인한다.
    if not _readable(dest):
        dest.unlink(missing_ok=True)
        raise VideoError("동영상을 열지 못했습니다. 손상되었거나 지원하지 "
                         "않는 코덱입니다.")

    log.info("동영상 임시 저장 camera=%s (%.1f MB)", camera_id, total / 1024 / 1024)
    return dest


def finalize(staged: Path, camera_id: str) -> str:
    """등록·수정이 성공한 뒤에만 부른다. 최종 이름으로 옮기고 이전 것을 지운다."""
    final = staged.with_suffix("")          # `.uploading` 을 뗀다
    staged.replace(final)
    _clear_previous(camera_id, keep=final)
    log.info("동영상 확정 camera=%s file=%s", camera_id, final.name)
    return f"{REL_PREFIX}/{final.name}"


def discard(staged: Path | None) -> None:
    """등록이 실패했을 때 임시 파일을 치운다."""
    if staged is not None:
        Path(staged).unlink(missing_ok=True)


def _readable(path: Path) -> bool:
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return True          # 확인할 수단이 없으면 통과시킨다
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return False
        ok, frame = cap.read()
        return bool(ok and frame is not None)
    finally:
        cap.release()


def info(rel_path: str) -> dict:
    """등록된 영상의 크기·길이·해상도. 화면에 보여 주기 위한 것."""
    out: dict = {"exists": False}
    if not rel_path:
        return out
    p = Path(rel_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_file():
        return out
    out["exists"] = True
    out["size_mb"] = round(p.stat().st_size / 1024 / 1024, 1)
    try:
        import cv2
        cap = cv2.VideoCapture(str(p))
        try:
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS) or 0
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                out["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                out["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                out["seconds"] = round(frames / fps, 1) if fps > 0 else 0
        finally:
            cap.release()
    except Exception:  # noqa: BLE001
        log.exception("동영상 정보를 읽지 못했습니다: %s", p)
    return out
