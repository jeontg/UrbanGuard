"""SVRDD → 2클래스 변환 (scripts/prepare_svrdd_yolo.py).

이 변환은 **틀려도 조용합니다.** 클래스 번호를 잘못 매핑하면 포트홀이 균열로
학습되는데, 학습은 정상적으로 끝나고 mAP 도 그럴듯하게 나옵니다. 몇 시간을
태우고 나서야 결과가 이상하다는 걸 알게 되고, 그때는 원인을 짚기가 매우
어렵습니다. 그래서 매핑만큼은 테스트로 못 박아 둡니다.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from tot_dashboard.common.config import PROJECT_ROOT


def _load_script():
    """scripts/ 는 패키지가 아니라 파일 경로로 불러온다."""
    path = PROJECT_ROOT / "scripts" / "prepare_svrdd_yolo.py"
    spec = importlib.util.spec_from_file_location("prepare_svrdd_yolo", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prepare_svrdd_yolo"] = mod
    spec.loader.exec_module(mod)
    return mod


P = _load_script()

# 이 픽스처가 쓰는 순서는 **논문 초록의 나열 순서**다. 실제 배포본 라벨의
# 순서와는 다르다(맨홀이 4번) — 그래서 이름으로 맞춰야 한다는 것을 보이려고
# 일부러 다른 순서를 쓴다. 포트홀이 3번이라 번호를 그대로 쓰면 엉뚱한 것이 된다.
SVRDD_NAMES = ["longitudinal crack", "transverse crack", "alligator crack",
               "pothole", "longitudinal patch", "transverse patch",
               "manhole cover"]


def _fixture(tmp_path: Path, labels: list[str]) -> Path:
    import cv2

    src = tmp_path / "src"
    (src / "images").mkdir(parents=True)
    (src / "labels").mkdir(parents=True)
    (src / "data.yaml").write_text(
        "train: ../train/images\nval: ../val/images\nnc: 7\n"
        f"names: {SVRDD_NAMES}\n".replace('"', "'"), encoding="utf-8")
    for i, lab in enumerate(labels):
        img = np.full((64, 64, 3), 40 + i * 10, dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        assert ok
        (src / "images" / f"{i:03d}.jpg").write_bytes(buf.tobytes())
        (src / "labels" / f"{i:03d}.txt").write_text(lab, encoding="utf-8")
    return src


def _convert(tmp_path: Path, labels: list[str], val_ratio: int = 10):
    src = _fixture(tmp_path, labels)
    out = tmp_path / "out"
    sys.argv = ["prepare_svrdd_yolo.py", "--src", str(src), "--out", str(out),
                "--val-ratio", str(val_ratio)]
    P.main()
    got = {}
    for txt in sorted(out.rglob("*.txt")):
        if txt.name == "data.yaml":
            continue
        got[txt.stem] = [l for l in txt.read_text(encoding="utf-8").splitlines() if l]
    return out, got


# --- 클래스 매핑 -------------------------------------------------------------

def test_포트홀은_0번_균열은_1번으로_간다(tmp_path):
    """SVRDD 에서 포트홀은 3번이다. 번호를 그대로 쓰면 안 된다."""
    _, got = _convert(tmp_path, [
        "3 0.4 0.4 0.1 0.1",     # pothole
        "0 0.5 0.5 0.2 0.2",     # longitudinal crack
        "2 0.3 0.7 0.2 0.1",     # alligator crack
    ], val_ratio=99)
    lines = [v[0] for v in got.values() if v]
    assert sorted(l.split()[0] for l in lines) == ["0", "1", "1"]
    # 좌표는 그대로 보존돼야 한다.
    pothole = next(l for l in lines if l.startswith("0 "))
    assert pothole == "0 0.4 0.4 0.1 0.1"


def test_패치와_맨홀은_손상이_아니라서_제외한다(tmp_path):
    _, got = _convert(tmp_path, [
        "4 0.6 0.6 0.1 0.1",     # longitudinal patch — 보수 흔적
        "5 0.6 0.6 0.1 0.1",     # transverse patch
        "6 0.2 0.2 0.1 0.1",     # manhole cover — 시설물
    ], val_ratio=99)
    assert all(v == [] for v in got.values())


def test_제외된_이미지는_배경으로_남긴다(tmp_path):
    """포트홀과 닮은 맨홀은 「이건 손상이 아니다」를 가르치는 표본이다.
    이미지째 버리면 그 학습 기회를 잃는다."""
    out, got = _convert(tmp_path, ["6 0.2 0.2 0.1 0.1"], val_ratio=99)
    assert len(list(out.rglob("*.jpg"))) == 1
    assert list(got.values()) == [[]]


def test_한_이미지에_섞여_있으면_손상만_남긴다(tmp_path):
    _, got = _convert(tmp_path,
                      ["3 0.4 0.4 0.1 0.1\n6 0.2 0.2 0.1 0.1\n1 0.7 0.7 0.1 0.1"],
                      val_ratio=99)
    lines = next(iter(got.values()))
    assert sorted(l.split()[0] for l in lines) == ["0", "1"]


# --- 안전장치 ---------------------------------------------------------------

def test_클래스_파일이_없으면_확인된_기본_순서를_쓴다(tmp_path, capsys):
    """배포본(SVRDD_YOLO.zip)에는 클래스 이름 파일이 없다. 그래서 라벨 박스를
    잘라 눈으로 확인한 순서를 기본값으로 쓰되, **쓰고 있다는 사실을 알린다.**"""
    import cv2

    src = tmp_path / "src"
    (src / "images").mkdir(parents=True)
    (src / "labels").mkdir(parents=True)
    ok, buf = cv2.imencode(".jpg", np.zeros((32, 32, 3), dtype=np.uint8))
    (src / "images" / "a.jpg").write_bytes(buf.tobytes())
    (src / "labels" / "a.txt").write_text("3 0.5 0.5 0.1 0.1", encoding="utf-8")
    out = tmp_path / "out"
    sys.argv = ["prepare_svrdd_yolo.py", "--src", str(src), "--out", str(out),
                "--val-ratio", "99"]
    P.main()
    msg = capsys.readouterr().out
    assert "확인된 기본 순서" in msg and "맨홀이 4번" in msg
    # 기본 순서에서도 3번은 포트홀이어야 한다.
    lab = next(p for p in out.rglob("*.txt"))
    assert lab.read_text(encoding="utf-8").startswith("0 ")


def test_확인된_순서에서_맨홀은_4번이다():
    """논문 초록의 나열 순서와 다르다. 초록대로 믿으면 맨홀을 균열로 배운다."""
    assert P.SVRDD_ORDER[3] == "pothole"
    assert P.SVRDD_ORDER[4] == "manhole cover"
    assert P.NAME_MAP["pothole"] == 0
    assert P.NAME_MAP["manhole cover"] is None


def test_배포본_분할이_있으면_그것을_따른다(tmp_path):
    """자체 분할을 쓰면 인접 프레임이 train·val 로 갈라져 성능이 부풀려진다."""
    import cv2

    src = tmp_path / "src"
    (src / "images").mkdir(parents=True)
    (src / "labels").mkdir(parents=True)
    for n in ("a", "b", "c"):
        ok, buf = cv2.imencode(".jpg", np.zeros((32, 32, 3), dtype=np.uint8))
        (src / "images" / f"{n}.jpg").write_bytes(buf.tobytes())
        (src / "labels" / f"{n}.txt").write_text("3 0.5 0.5 0.1 0.1", encoding="utf-8")
    # 배포본은 Windows 구분자로 적혀 있다.
    (src / "train.txt").write_text("images\\a.jpg\n", encoding="utf-8")
    (src / "val.txt").write_text("images\\b.jpg\n", encoding="utf-8")
    (src / "test.txt").write_text("images\\c.jpg\n", encoding="utf-8")
    out = tmp_path / "out"
    sys.argv = ["prepare_svrdd_yolo.py", "--src", str(src), "--out", str(out)]
    P.main()
    named = lambda s: sorted(p.stem for p in (out / s / "images").glob("*.jpg"))
    assert named("train") == ["images_a"]
    assert named("val") == ["images_b"]
    assert named("test") == ["images_c"]


def test_모르는_클래스는_경고하고_버린다(tmp_path, capsys):
    src = _fixture(tmp_path, ["0 0.5 0.5 0.2 0.2"])
    (src / "data.yaml").write_text(
        "nc: 2\nnames: ['알수없는것', 'pothole']\n", encoding="utf-8")
    sys.argv = ["prepare_svrdd_yolo.py", "--src", str(src),
                "--out", str(tmp_path / "out"), "--val-ratio", "99"]
    P.main()
    assert "매핑을 모르는 클래스" in capsys.readouterr().out


def test_분할은_난수를_쓰지_않아_재현된다(tmp_path):
    """다시 돌렸을 때 분할이 달라지면 학습 결과를 비교할 수 없다."""
    labels = [f"3 0.{i} 0.5 0.1 0.1" for i in range(1, 9)]
    out1, _ = _convert(tmp_path / "a", labels, val_ratio=4)
    out2, _ = _convert(tmp_path / "b", labels, val_ratio=4)
    names = lambda o: sorted(p.name for p in (o / "val" / "images").glob("*.jpg"))
    assert names(out1) == names(out2)
    assert names(out1), "검증셋이 비어 있다"


def test_data_yaml은_2클래스로_쓴다(tmp_path):
    out, _ = _convert(tmp_path, ["3 0.4 0.4 0.1 0.1"], val_ratio=99)
    text = (out / "data.yaml").read_text(encoding="utf-8")
    assert "nc: 2" in text
    assert "['pothole', 'crack']" in text
