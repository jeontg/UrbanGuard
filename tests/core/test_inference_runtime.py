"""추론 런타임 로더 (core/inference.py)와 변환 후보 인식 (core/model_registry.py).

이 시험들은 **2026-08-17 실측 중 실제로 밟은 지뢰**를 못박아 둔 것이다.
셋 다 「조용히 틀린 결과가 나오는」 종류라 사람 눈으로는 안 잡힌다.

* **task 를 명시하지 않으면 분할 모델이 검출로 열린다** — 마스크 계수 32개를
  클래스 점수로 읽어 신뢰도가 1을 넘고 상자가 300개씩 나온다. 그 상태로
  속도를 재면 1.10배라는 틀린 값이 나온다(실제로 그랬다)
* **변환 산출물이 목록에서 빠지면 비교 자체가 불가능하다** — ONNX·OpenVINO 는
  ``.pt`` 가 아니라 기존 탐색에 걸리지 않았다
* **변환 후보를 운영 모델과 헷갈리면 안 된다** — 「화면에서는 바꿨는데 실제로는
  예전 모델이 돌고 있는」 상태를 만들지 않는다
* **도메인·백엔드는 원본에서 물려받는다** — 파일명만 보고 추측하면
  ``best.onnx`` 가 분할인지 검출인지 알 수 없다
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tot_dashboard.core import inference as INF
from tot_dashboard.core import model_registry as MR


# --- 런타임 판정 -------------------------------------------------------------
def test_경로로_런타임을_가린다(tmp_path):
    assert INF.runtime_of("models/best.pt") == INF.PT
    assert INF.runtime_of("models/best.onnx") == INF.ONNX

    ov = tmp_path / f"best{INF.OPENVINO_SUFFIX}"
    ov.mkdir()
    assert INF.runtime_of(ov) == INF.OPENVINO


def test_모델로_받을_경로만_받는다(tmp_path):
    assert INF.is_model_path("a/best.pt")
    assert INF.is_model_path("a/best.onnx")
    assert not INF.is_model_path("a/readme.txt"), "아무 파일이나 모델로 보면 안 된다"

    ov = tmp_path / f"m{INF.OPENVINO_SUFFIX}"
    ov.mkdir()
    assert INF.is_model_path(ov)

    plain = tmp_path / "runs"
    plain.mkdir()
    assert not INF.is_model_path(plain), "아무 폴더나 OpenVINO 로 보면 안 된다"


def test_백엔드에서_task를_뽑는다():
    assert INF.task_of(MR.YOLO_SEG) == "segment"
    assert INF.task_of(MR.YOLO_DET) == "detect"


# --- task 강제 — 가장 중요한 시험 --------------------------------------------
def test_task를_비우면_거부한다():
    """기본값을 두지 않는다.

    기본값이 있으면 부르는 쪽이 안 넘기게 되고, 그 순간 분할 모델이 검출로
    열린다. **거부가 잘못된 결과보다 낫다.**
    """
    with pytest.raises(ValueError) as e:
        INF.load("models/best.pt", task="")
    assert "task" in str(e.value)


def test_없는_경로는_명확히_알린다():
    with pytest.raises(FileNotFoundError):
        INF.load("models/there_is_no_such_model.pt", task="detect")


# --- 예열 --------------------------------------------------------------------
def test_예열_횟수는_런타임마다_다르다():
    """OpenVINO 는 3회로 부족하다 — 실측에서 17회째에 안정됐다."""
    assert INF.WARMUP_RUNS[INF.OPENVINO] > INF.WARMUP_RUNS[INF.ONNX], (
        "OpenVINO 예열이 ONNX 이하면 실측 결과와 어긋난다")
    assert INF.WARMUP_RUNS[INF.OPENVINO] >= 17, (
        "실측상 17회째에 안정됐다. 그보다 적으면 첫 실탐지가 10배 느리다")


def test_실측_배수는_기록으로_남아_있다():
    """제안서 숫자의 출처가 코드에 있어야 한다."""
    assert INF.MEASURED_SPEEDUP[INF.PT] == 1.0
    assert INF.MEASURED_SPEEDUP[INF.ONNX] > 1.0
    assert INF.MEASURED_SPEEDUP[INF.OPENVINO] > INF.MEASURED_SPEEDUP[INF.ONNX]


# --- 레지스트리의 변환 후보 인식 ---------------------------------------------
def _converted() -> list:
    return [m for m in MR.all_models() if "candidates" in m.key]


def test_변환_후보는_운영으로_표시되지_않는다():
    """후보가 하나도 없을 수 있다(변환 전). 있으면 반드시 운영과 구분돼야 한다."""
    for m in _converted():
        assert not m.in_use, f"{m.key} 가 운영 중으로 표시됐다"
        assert "운영에 적용되어 있지 않습니다" in m.note, (
            f"{m.key} 의 설명에 운영 미적용 표시가 없다")


def test_변환_후보는_원본에서_도메인과_백엔드를_물려받는다():
    """파일명 추측에 맡기면 best.onnx 가 분할인지 검출인지 알 수 없다.

    ⚠️ 2026-08-22: 도메인이 **비어 있는 것 자체는 문제가 아니다** — 범용
    YOLO 검출기(yolo26n.pt 등)는 원본 자체가 분류 미상("")이고, 후보도
    그 상태를 그대로 물려받아야 옳다(전수점검으로 범용 이름을 crowd
    열쇠말에서 뺀 뒤 생긴 정상적인 상태 — `test_model_registry.py`의
    `test_범용_YOLO_버전_이름만으로는_도메인을_못_박지_않는다` 참고).
    이 시험의 핵심은 backend(분할/검출 구분)이지, domain 이 항상 채워져
    있어야 한다는 것이 아니다.
    """
    for m in _converted():
        assert m.backend in (MR.YOLO_SEG, MR.YOLO_DET, MR.TV_SEG)
        # task 로 풀 수 있어야 로더에 넘길 수 있다.
        assert INF.task_of(m.backend) in ("segment", "detect")


def test_변환_후보의_라벨에_런타임이_드러난다():
    for m in _converted():
        assert any(lbl in m.label for lbl in INF.RUNTIME_LABELS.values()), (
            f"{m.key} 라벨에 런타임 표시가 없다: {m.label}")


def test_변환_후보도_키로_찾을_수_있다():
    for m in _converted():
        assert MR.get(m.key) is not None, f"{m.key} 를 get() 이 못 찾는다"


def test_후보_폴더의_pt도_목록에_나온다():
    """받아 놓고 목록에서 안 보이면 비교할 방법이 없다.

    기존 규칙은 ``best.pt`` · ``last.pt`` 만 받았는데, 그 탓에
    ``models/candidates/yolo26n.pt`` 가 통째로 숨었다. 사람이 후보 폴더에
    일부러 넣은 파일은 이름과 무관하게 받는다.
    """
    from tot_dashboard.common.config import PROJECT_ROOT

    folder = PROJECT_ROOT / "models" / "candidates"
    on_disk = {p.name for p in folder.glob("*.pt")} if folder.is_dir() else set()
    if not on_disk:
        pytest.skip("후보 폴더에 .pt 가 없다")
    listed = {Path(m.key).name for m in _converted()}
    assert on_disk <= listed, f"목록에서 빠진 후보: {on_disk - listed}"


def test_운영_기본값은_변환_후보로_바뀌지_않는다():
    """후보가 목록에 들어왔다고 기본 선택이 바뀌면 안 된다."""
    for dom in ("flood", "crowd", "road"):
        d = MR.default_for(dom)
        if d is None:
            continue
        assert "candidates" not in d.key, (
            f"{dom} 기본값이 변환 후보({d.key})로 잡혔다")


# --- 고른 모델이 실제로 쓰이는가 ---------------------------------------------
#
# 지금까지 화면에서 골라도 **침수·인파는 아무 일도 일어나지 않았다.** 설정은
# 저장되는데 읽는 코드가 없었다. 「바뀐 줄 알고 관제하는」 상태라 가장 나쁘다.

def test_침수는_설정이_없으면_설정파일_값을_쓴다(monkeypatch):
    from tot_dashboard.service import runner

    monkeypatch.setattr("tot_dashboard.core.model_ops.selected_key",
                        lambda *a, **k: "")
    assert runner._flood_model_rel("models/best.pt") == "models/best.pt"


def test_침수는_고른_모델을_우선한다(monkeypatch):
    from tot_dashboard.service import runner

    monkeypatch.setattr("tot_dashboard.core.model_ops.selected_key",
                        lambda *a, **k: "models/yolo11s.pt")
    assert runner._flood_model_rel("models/best.pt") == "models/yolo11s.pt"


def test_침수는_없는_파일을_고르면_설정파일_값으로_되돌아간다(monkeypatch):
    """고를 수 없게 되는 것보다 예전 모델로 도는 편이 낫다."""
    from tot_dashboard.service import runner

    monkeypatch.setattr("tot_dashboard.core.model_ops.selected_key",
                        lambda *a, **k: "models/there_is_no_such.pt")
    assert runner._flood_model_rel("models/best.pt") == "models/best.pt"


def test_침수는_설정_조회가_실패해도_뜬다(monkeypatch):
    """DB 가 없어도 파이프라인은 떠야 한다."""
    from tot_dashboard.service import runner

    def boom(*a, **k):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr("tot_dashboard.core.model_ops.selected_key", boom)
    assert runner._flood_model_rel("models/best.pt") == "models/best.pt"


def test_마스킹은_고른_인파_모델을_우선한다(monkeypatch):
    from tot_dashboard.core import image_mask

    monkeypatch.setattr("tot_dashboard.core.model_ops.selected_key",
                        lambda *a, **k: "models/best.pt")
    assert image_mask._mask_model_rel() == "models/best.pt"


def test_마스킹은_없는_파일을_고르면_기본값으로_되돌아간다(monkeypatch):
    """마스킹이 멈추면 개인정보가 가려지지 않은 채 저장될 수 있다."""
    from tot_dashboard.core import image_mask

    monkeypatch.setattr("tot_dashboard.core.model_ops.selected_key",
                        lambda *a, **k: "models/there_is_no_such.pt")
    assert image_mask._mask_model_rel() == image_mask.DEFAULT_MASK_MODEL


def test_마스킹_모델_캐시를_비울_수_있다():
    """캐시를 안 비우면 프로세스가 사는 동안 예전 모델이 계속 쓰인다."""
    from tot_dashboard.core import image_mask

    image_mask._model_tried = True
    image_mask.reset_model_cache()
    assert image_mask._model_tried is False
    assert image_mask._model is None


def test_인파_안내문이_계수는_안_바뀐다고_말한다():
    """마스킹만 바뀌고 밀집도는 안 바뀐다. 그 차이를 숨기면 안 된다."""
    from tot_dashboard.core import model_ops

    note = model_ops.APPLY_NOTE["crowd"]
    assert "계수" in note and "바뀌지 않습니다" in note


# --- 실제 모델이 있을 때만 도는 시험 -----------------------------------------
#
# 변환 산출물은 저장소에 커밋하지 않는다(수십 MB). 있으면 확인하고 없으면 건너뛴다.

def _pair() -> tuple[Path, Path] | None:
    from tot_dashboard.common.config import PROJECT_ROOT

    pt = PROJECT_ROOT / "models" / "best.pt"
    onnx = PROJECT_ROOT / "models" / "candidates" / "best.onnx"
    return (pt, onnx) if pt.is_file() and onnx.is_file() else None


@pytest.mark.skipif(_pair() is None, reason="변환 산출물이 없다")
def test_분할_모델은_분할로_열린다():
    """이것이 무너지면 결과가 조용히 틀린다."""
    pt, onnx = _pair()
    net = INF.load(onnx, task="segment", warmup=False)
    assert net.task == "segment", (
        "ONNX 가 검출로 열렸다 — 마스크 계수를 클래스 점수로 읽는 상태다")


@pytest.mark.skipif(_pair() is None, reason="변환 산출물이 없다")
def test_변환_전후_검출_결과가_같다():
    """빨라져도 답이 달라지면 쓸 수 없다.

    **상자를 IoU 로 짝지어** 비교한다. 신뢰도 내림차순으로 자리끼리 맞대면,
    신뢰도가 같은 상자의 순서가 뒤집혀 좌표가 수백 px 달라 보인다
    (실측 때 590px 차이로 나왔는데 실제로는 같은 상자였다).
    """
    import numpy as np

    pt_path, onnx_path = _pair()
    frame = _sample_frame()
    if frame is None:
        pytest.skip("비교할 프레임이 없다")

    a = _boxes(INF.load(pt_path, task="segment", warmup=False), frame)
    b = _boxes(INF.load(onnx_path, task="segment", warmup=False), frame)

    assert len(a) == len(b), f"검출 개수가 다르다: PyTorch {len(a)} / ONNX {len(b)}"
    if not len(a):
        return
    for row in a:
        best = max(_iou(row, other) for other in b)
        assert best >= 0.99, f"짝지을 상자가 없다 (최대 IoU {best:.4f})"


def _sample_frame():
    import cv2

    from tot_dashboard.common.config import PROJECT_ROOT

    for p in sorted((PROJECT_ROOT / "data").rglob("*.jpg"))[:20]:
        img = cv2.imread(str(p))
        if img is not None:
            return cv2.resize(img, (640, 640))
    return None


def _boxes(net, frame):
    import numpy as np

    r = INF.predict(net, frame)[0]
    if r.boxes is None or len(r.boxes) == 0:
        return np.zeros((0, 4))
    return r.boxes.xyxy.cpu().numpy()


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(x2 - x1, 0) * max(y2 - y1, 0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / ua) if ua > 0 else 0.0
