"""추론 런타임 로더 — task 명시 · 장치 고정 · 예열을 한곳에서 (S-61).

왜 필요한가
    같은 가중치라도 **어떤 런타임으로 실행하느냐에 따라 속도가 크게 다릅니다.**
    2026-08-17 실측(i7-1360P, 96회 추론, 침수 분할 모델)에서

        PyTorch 539.7ms  →  ONNX 267.4ms(2.0배)  →  OpenVINO 74.8ms(7.2배)

    였고 **검출 결과는 완전히 같았습니다**(IoU 1.0000, 신뢰도 차이 0.0000).
    정확도를 잃지 않고 속도만 얻으므로 쓸 이유가 분명합니다.

    그런데 그 실측 과정에서 **함정 세 가지**를 만났고, 셋 다 호출하는 쪽이
    매번 신경 써야 하는 종류였습니다. 그래서 여기 한곳에 모읍니다.

세 가지 함정

    1. **task 메타데이터 유실** — 내보낸 파일에는 task 정보가 없을 수 있다.
       분할 모델이 검출로 로드되면 **마스크 계수 32개를 클래스 점수로 읽어**
       신뢰도가 1을 넘고 상자가 300개씩 나온다. 겉보기에는 그냥 「빨라졌는데
       결과가 좀 다르네」로 보여 **놓치기 쉽다.**

    2. **내장 GPU 커널 컴파일** — 장치를 안 정해 주면 OpenVINO 가 내장 GPU
       경로를 타려다 실패하는 경우가 있다. 실제로 한 회차가 **499초**까지
       늘어졌고 커널 오류 로그가 남았다.

    3. **예열** — OpenVINO 는 첫 두 바퀴가 약 950ms 이고 그 뒤 70~76ms 로
       내려앉는다. 예열 없이 기동하면 **가장 급한 초기 구간이 가장 느리다.**

무엇을 하지 않는가
    **운영 파이프라인을 바꾸지 않습니다.** 이 모듈은 시험 탐지
    (:mod:`.model_probe`) 경로에서만 씁니다. 상시 탐지가 쓰는 로더를 갈아
    끼우는 것은 워처 재구성이 얽혀 있어 별도 작업입니다.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("urbanguard.inference")

# --- 런타임 --------------------------------------------------------------
PT = "pytorch"
ONNX = "onnx"
OPENVINO = "openvino"

RUNTIME_LABELS = {
    PT: "PyTorch (기본)",
    ONNX: "ONNX Runtime",
    OPENVINO: "OpenVINO",
}

# 실측 배수 — 침수 분할 모델 기준(2026-08-17, i7-1360P, 96회 추론).
# **참고값이다.** 납품 서버 CPU 가 다르면 달라진다.
MEASURED_SPEEDUP = {PT: 1.0, ONNX: 2.0, OPENVINO: 7.2}

# OpenVINO 내보내기 산출물은 파일이 아니라 폴더다.
OPENVINO_SUFFIX = "_openvino_model"

# 예열 횟수 — **런타임마다 다르다. 실측해서 정했다.**
#
# 640x640 빈 프레임으로 40회 연속 추론하며 언제 안정되는지 쟀다
# (2026-08-17, i7-1360P):
#
#   OpenVINO  1회 7,441ms → 16회까지 400~1,150ms → **17회째부터 54ms** 고정
#   ONNX      1회 1,024ms → **2회째부터 190~250ms**
#
# OpenVINO 는 세 번만 돌려서는 전혀 준비가 안 된다. 실제로 3회 예열 후 첫
# 추론이 609ms 였다(안정값의 11배). 여유를 두어 20회로 잡는다 — 기동 시
# 십수 초가 들지만, 그 대가로 **첫 실탐지부터 제 속도**가 난다.
WARMUP_RUNS = {PT: 3, ONNX: 3, OPENVINO: 20}
DEFAULT_WARMUP_RUNS = 3

# 예열용 입력 크기. 실제 추론 크기와 같아야 그래프가 재컴파일되지 않는다.
WARMUP_IMGSZ = 640


def runtime_of(path: str | Path) -> str:
    """경로만 보고 런타임을 판정한다."""
    p = Path(path)
    if p.name.endswith(OPENVINO_SUFFIX) or p.is_dir():
        return OPENVINO
    if p.suffix.lower() == ".onnx":
        return ONNX
    return PT


def is_model_path(path: str | Path) -> bool:
    """레지스트리가 후보로 받아도 되는 경로인가."""
    p = Path(path)
    if p.is_dir():
        return p.name.endswith(OPENVINO_SUFFIX)
    return p.suffix.lower() in (".pt", ".onnx")


def task_of(backend: str) -> str:
    """레지스트리 백엔드 값 → ultralytics task.

    :mod:`.model_registry` 의 ``YOLO_SEG`` / ``YOLO_DET`` 를 그대로 받는다.
    **추측하지 않는다** — 모르면 검출로 떨어뜨리는 대신 호출하는 쪽이
    명시하게 한다.
    """
    from . import model_registry as reg

    if backend == reg.YOLO_SEG:
        return "segment"
    return "detect"


def load(path: str | Path, *, task: str, warmup: bool = True):
    """추론 모델을 연다. **task 는 반드시 넘겨야 한다.**

    :param task: ``"segment"`` 또는 ``"detect"``. 내보낸 파일에는 이 정보가
        없을 수 있어 **여기서 못박지 않으면 분할 모델이 검출로 열린다.**
    :param warmup: 열자마자 빈 프레임으로 몇 번 돌려 그래프를 준비한다.
    :raises FileNotFoundError: 경로가 없을 때.
    :raises ValueError: ``task`` 가 비었을 때 — 기본값을 두지 않는다.
    """
    if not task:
        raise ValueError(
            "task 를 명시해야 합니다('segment' 또는 'detect'). "
            "내보낸 모델은 task 정보를 잃을 수 있고, 분할 모델이 검출로 "
            "열리면 마스크 계수를 클래스 점수로 읽습니다.")

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"모델 경로가 없습니다: {p}")

    from ultralytics import YOLO

    net = YOLO(str(p), task=task)

    # ultralytics 가 task 를 되돌려 주면 대조한다. 다르면 위 1번 함정이다.
    actual = getattr(net, "task", task)
    if actual != task:
        raise ValueError(
            f"모델이 요청한 task 로 열리지 않았습니다: 요청 {task} / 실제 {actual} "
            f"({p.name}). 이 상태로 추론하면 결과가 조용히 틀립니다.")

    if warmup:
        _warmup(net, p)
    return net


def _warmup(net, path: Path) -> None:
    """빈 프레임으로 런타임에 맞는 횟수만큼 돌려 둔다.

    예열 실패는 **치명적이지 않다** — 첫 실탐지가 느릴 뿐이다. 그래서
    삼키되 흔적은 남긴다.
    """
    runtime = runtime_of(path)
    runs = WARMUP_RUNS.get(runtime, DEFAULT_WARMUP_RUNS)
    try:
        import numpy as np

        blank = np.zeros((WARMUP_IMGSZ, WARMUP_IMGSZ, 3), dtype=np.uint8)
        for _ in range(runs):
            net.predict(blank, imgsz=WARMUP_IMGSZ, verbose=False, device="cpu")
        log.info("모델 예열 완료: %s (%s · %d회)",
                 path.name, RUNTIME_LABELS.get(runtime, runtime), runs)
    except Exception as e:  # noqa: BLE001
        log.warning("모델 예열 실패(계속 진행): %s — %s", path.name, str(e)[:200])


def predict(net, frame, *, imgsz: int = WARMUP_IMGSZ, **kw):
    """추론. **장치를 CPU 로 고정한다.**

    장치를 비워 두면 OpenVINO 가 내장 GPU 경로를 타려다 커널 컴파일에 실패해
    한 번에 수백 초가 걸린 사례가 있었다. 우리는 GPU 를 쓰지 않으므로
    처음부터 못박는다.
    """
    kw.setdefault("verbose", False)
    return net.predict(frame, imgsz=imgsz, device="cpu", **kw)
