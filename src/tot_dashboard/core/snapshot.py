"""ROI 설정용 정지영상 취득.

ROI는 **설정 작업**이지 관제가 아니다. 그런데 지금까지는 탐지 파이프라인이
저장해 둔 프레임에만 의존해서, 상시 탐지가 켜진 침수 카메라 말고는 화면이
비었다. 동영상을 새로 등록하면 ROI를 그릴 수도 없었다.

그래서 **소스에서 직접 한 장 뽑는다.** 파이프라인이 돌든 말든 상관없다.
"""
from __future__ import annotations

import base64
import logging

from ..common.config import PROJECT_ROOT

log = logging.getLogger("urbanguard.snapshot")

# HLS는 연결 직후 빈 프레임이 몇 장 온다. 그만큼 더 읽는다.
HLS_TRIES = 40
# 동영상은 맨 앞이 검은 화면이거나 페이드인인 경우가 많아 조금 들어가서 잡는다.
VIDEO_SEEK_RATIO = 0.15


def _encode(frame) -> str:
    import cv2
    ok, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""


def _from_video(path_str: str):
    """동영상 파일에서 한 장. 경로는 프로젝트 폴더 기준 상대경로도 받는다."""
    import cv2
    from pathlib import Path

    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_file():
        return None, f"동영상 파일을 찾을 수 없습니다: {path_str}"

    cap = cv2.VideoCapture(str(p))
    if not cap.isOpened():
        cap.release()
        return None, "동영상을 열지 못했습니다. 형식을 확인하세요."
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 10:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * VIDEO_SEEK_RATIO))
        ok, frame = cap.read()
        if not ok or frame is None:
            # 되감기가 실패하는 코덱이 있다. 처음부터 다시 읽어 본다.
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            return None, "동영상에서 화면을 읽지 못했습니다."
        return frame, ""
    finally:
        cap.release()


def _from_stream(url: str):
    import cv2
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return None, ("스트림을 열지 못했습니다. 주소와 네트워크를 확인하세요.")
    try:
        for _ in range(HLS_TRIES):
            ok, frame = cap.read()
            if ok and frame is not None:
                return frame, ""
        return None, "스트림에서 화면을 받지 못했습니다."
    finally:
        cap.release()


def grab(cam, *, mask: bool = True) -> tuple[str, str, str]:
    """카메라에서 정지영상 한 장.

    반환: (base64 JPEG, 마스킹 상태, 오류 메시지)

    ⚠️ 인파 관측 스냅샷과 달리 **마스킹에 실패해도 화면을 내보낸다.**
    ROI는 영상이 없으면 그릴 수가 없어 기능 자체가 성립하지 않기 때문이다.
    대신 가리지 못한 사실을 상태로 돌려주고 화면에 경고를 띄운다.
    이 화면은 설정 권한자만 접근한다.
    """
    try:
        import cv2  # noqa: F401
    except Exception:  # noqa: BLE001
        return "", "", "영상 라이브러리를 쓸 수 없습니다."

    stype = getattr(cam, "source_type", "")
    if stype == "video":
        frame, err = _from_video(getattr(cam, "source_path", "") or "")
    elif stype == "hls":
        url = getattr(cam, "source_url", "") or ""
        if not url:
            return "", "", "이 카메라에 스트림 주소가 없습니다."
        frame, err = _from_stream(url)
    else:
        return "", "", ("합성(synthetic) 소스는 정지영상이 없습니다. "
                        "ROI를 그리려면 실제 영상 소스가 필요합니다.")
    if frame is None:
        return "", "", err

    mask_status = ""
    if mask:
        try:
            from . import image_mask
            mask_status, _ = image_mask.mask_array(frame)
        except Exception:  # noqa: BLE001
            log.exception("정지영상 마스킹 실패 camera=%s", getattr(cam, "id", "?"))
            mask_status = "failed"

    return _encode(frame), mask_status, ""
