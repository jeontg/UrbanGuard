"""제보 사진 개인정보 마스킹.

노면 제보 사진에는 지나가는 사람과 차량번호판이 함께 찍힌다. 그대로 저장하면
개인정보를 수집·보관하는 셈이 되고, 그 사진이 학습 데이터로 흘러가면 문제가
더 커진다(설계서 9-5절).

**노면 손상 학습에 사람은 필요 없으므로 가려도 목적을 해치지 않는다.** 그래서
원본을 남기지 않고 **받는 즉시 가려서 저장**한다.

⚠️ 한계 — 사람은 기존 객체 검출 모델로 가리지만 **차량번호판은 전용 모델이
없어 자동으로 가리지 못한다.** 담당자 확인 단계에서 육안으로 확인해야 하며,
이 사실을 화면에 표시한다.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("urbanguard.mask")

# 상태
MASKED = "masked"              # 사람 영역을 가림
NO_TARGET = "no_target"        # 가릴 대상이 없었음
UNAVAILABLE = "unavailable"    # 검출기를 쓸 수 없어 가리지 못함
FAILED = "failed"
STATUS_LABELS = {
    MASKED: "사람 영역 마스킹됨",
    NO_TARGET: "가릴 사람 없음",
    UNAVAILABLE: "마스킹 미적용 (검출기 없음)",
    FAILED: "마스킹 실패",
}
# 자동 마스킹이 되지 않은 상태 — 담당자 육안 확인이 필요하다.
NEEDS_REVIEW = {UNAVAILABLE, FAILED}

_model = None
_model_tried = False


def _load_model():
    """객체 검출 모델을 지연 로드한다. 없으면 None."""
    global _model, _model_tried
    if _model_tried:
        return _model
    _model_tried = True
    try:
        from ..common.config import PROJECT_ROOT
        from . import inference

        path = PROJECT_ROOT / _mask_model_rel()
        if not path.exists():
            log.warning("마스킹용 모델이 없습니다: %s", path)
            return None
        # 공통 로더 — ONNX·OpenVINO 후보를 골라도 task·장치·예열이 처리된다.
        _model = inference.load(path, task="detect")
    except Exception:  # noqa: BLE001
        log.exception("마스킹용 모델을 불러오지 못했습니다")
        _model = None
    return _model


# 코드 기본값. 설정이 비었거나 그 파일이 없으면 여기로 되돌아간다.
DEFAULT_MASK_MODEL = "models/yolo11s.pt"


def _mask_model_rel() -> str:
    """마스킹에 쓸 모델 경로. **화면에서 고른 인파 모델이 우선한다.**

    레지스트리의 ``crowd`` 항목이 가리키는 실제 소비처가 여기다. 지금까지는
    경로가 박혀 있어 **화면에서 골라도 아무 일도 일어나지 않았다.**

    파일이 없으면 기본값으로 되돌아간다 — 마스킹이 아예 멈추면 개인정보가
    가려지지 않은 채 저장될 수 있어, **고른 모델이 없을 때 멈추는 것이 더
    위험하다.**
    """
    try:
        from . import model_ops

        chosen = (model_ops.selected_key("crowd") or "").strip()
        if chosen:
            from ..common.config import PROJECT_ROOT

            if (PROJECT_ROOT / chosen).exists():
                return chosen
            log.warning("지정된 인파 모델을 쓸 수 없어 기본값으로 갑니다: %s", chosen)
    except Exception as e:  # noqa: BLE001
        log.warning("인파 모델 설정 조회 실패(기본값 사용): %s", str(e)[:120])
    return DEFAULT_MASK_MODEL


def reset_model_cache() -> None:
    """모델을 다시 고르게 한다. 설정이 바뀐 뒤 호출한다."""
    global _model, _model_tried
    _model, _model_tried = None, False


def mask_array(img, *, conf: float = 0.25, pad_ratio: float = 0.06) -> tuple[str, int]:
    """BGR 배열에서 사람 영역을 **제자리로** 가린다. 반환: (상태, 가린 수).

    파일이 아닌 배열을 받는 이유는, CCTV 관측 스냅샷처럼 디스크에 떨어뜨리지
    않는 화면도 같은 기준으로 가려야 하기 때문이다. 원본을 남기지 않는다는
    원칙은 파일이든 메모리든 동일하다.
    """
    try:
        import cv2
    except Exception:  # noqa: BLE001
        log.exception("영상 라이브러리를 쓸 수 없습니다")
        return FAILED, 0

    model = _load_model()
    if model is None:
        return UNAVAILABLE, 0

    n = 0
    try:
        h, w = img.shape[:2]
        res = model.predict(img, conf=conf, verbose=False)[0]
        names = getattr(res, "names", {}) or {}
        for box in (res.boxes or []):
            cls = int(box.cls[0]) if box.cls is not None else -1
            if names.get(cls) != "person":
                continue
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
            # 검출 상자가 몸을 살짝 잘라내는 경우가 있어 여유를 준다.
            px, py = int((x2 - x1) * pad_ratio), int((y2 - y1) * pad_ratio)
            x1, y1 = max(0, x1 - px), max(0, y1 - py)
            x2, y2 = min(w, x2 + px), min(h, y2 + py)
            if x2 <= x1 or y2 <= y1:
                continue
            roi = img[y1:y2, x1:x2]
            # 강한 픽셀화 — 흐림만으로는 복원 가능성이 남는다.
            small = cv2.resize(roi, (max(1, (x2 - x1) // 18),
                                     max(1, (y2 - y1) // 18)),
                               interpolation=cv2.INTER_LINEAR)
            img[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1),
                                           interpolation=cv2.INTER_NEAREST)
            n += 1
    except Exception:  # noqa: BLE001
        log.exception("마스킹 처리 중 오류")
        return FAILED, 0
    return (MASKED if n else NO_TARGET), n


def mask_people(src: Path, dest: Path, *, conf: float = 0.25,
                pad_ratio: float = 0.06) -> tuple[str, int]:
    """사진에서 사람 영역을 흐리게 처리해 dest 에 저장한다.

    반환: (상태, 가린 영역 수)

    마스킹에 실패하면 **원본을 그대로 복사하지 않는다.** 대신 상태를 남겨
    담당자가 확인하기 전까지 화면에서 경고를 띄운다.
    """
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        log.exception("영상 라이브러리를 쓸 수 없습니다")
        return FAILED, 0

    try:
        # 한글 경로에서 cv2.imread 가 조용히 실패하므로 바이트로 읽어 디코드한다.
        buf = np.fromfile(str(src), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return FAILED, 0
    except Exception:  # noqa: BLE001
        log.exception("이미지를 읽지 못했습니다: %s", src)
        return FAILED, 0

    status, n = mask_array(img, conf=conf, pad_ratio=pad_ratio)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok, enc = cv2.imencode(dest.suffix or ".jpg", img)
        if not ok:
            return FAILED, 0
        enc.tofile(str(dest))
    except Exception:  # noqa: BLE001
        log.exception("마스킹 결과를 저장하지 못했습니다: %s", dest)
        return FAILED, 0
    return status, n
