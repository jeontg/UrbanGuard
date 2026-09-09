"""노면 학습 데이터 자동 수집 (road/dataset_collector.py, Phase 3).

지켜야 할 것은 네 가지다.

* **기본은 꺼짐** — 도로 영상을 디스크에 쌓는 일은 운영 판단이어야 한다
* **마스킹 실패 시 저장 안 함** — 오래 보관하는 파일이라 「나중에 확인」이 안 된다
* **상한** — 지점당 하루·최소 간격·디스크 총량
* **호출자를 막지 않음** — 수집 때문에 관제 분석이 멈추면 본말이 전도된다
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest

from tot_dashboard.core import image_mask
from tot_dashboard.road import dataset_collector as DC


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """수집 경로를 임시 폴더로 돌리고 상태를 비운다."""
    monkeypatch.setattr(DC, "DATASET_DIR", tmp_path / "raw")
    monkeypatch.setattr(DC, "is_enabled", lambda: True)
    DC.reset_state()
    yield tmp_path / "raw"
    DC.reset_state()


@pytest.fixture()
def masked_ok(monkeypatch):
    """마스킹이 정상 동작하는 상황."""
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.NO_TARGET, 0))


def _meta(res) -> list[dict]:
    """업로드 결과 폴더의 meta.jsonl 을 읽는다."""
    lines = (pathlib.Path(res["dir"]) / DC.META_NAME).read_text(
        encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines]


def frames(n=4, w=320, h=240):
    return [(i, np.full((h, w, 3), 60 + i, dtype=np.uint8)) for i in range(n)]


# --- 기본값 -----------------------------------------------------------------

def test_기본은_꺼짐이다(monkeypatch):
    """켜는 것은 개인정보 검토를 거친 운영 판단이어야 한다."""
    from tot_dashboard.core import settings as S
    assert S.DEFAULTS[S.KEY_ROAD_COLLECT] == "off"


def test_꺼져_있으면_아무것도_저장하지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(DC, "DATASET_DIR", tmp_path / "raw")
    monkeypatch.setattr(DC, "is_enabled", lambda: False)
    out = DC.collect("CAM-A", "가지점", frames())
    assert out["saved"] == 0 and out["reason"] == DC.SKIP_DISABLED
    assert not (tmp_path / "raw").exists()


# --- 저장 -------------------------------------------------------------------

def test_관측_프레임을_저장한다(sandbox, masked_ok):
    out = DC.collect("CAM-A", "가지점", frames())
    assert out["saved"] == DC.MAX_PER_ROUND
    assert len(list((sandbox / "CAM-A").glob("*.jpg"))) == DC.MAX_PER_ROUND


def test_출처와_마스킹_상태를_함께_남긴다(sandbox, masked_ok):
    """이미지만 쌓으면 몇 달 뒤에 언제 어디서 받은 것인지 알 수 없다."""
    DC.collect("CAM-A", "가지점", frames(), source="focus")
    meta = (sandbox / "CAM-A" / DC.META_NAME).read_text(encoding="utf-8")
    rec = json.loads(meta.splitlines()[0])
    assert rec["camera_name"] == "가지점"
    assert rec["source"] == "focus"
    assert rec["mask_status"] == image_mask.NO_TARGET
    # 번호판은 가리지 못한다 — 그 사실이 파일 옆에 남아야 반출 전에 확인한다.
    assert rec["plate_masked"] is False


def test_한_관측에서_남기는_장수를_제한한다(sandbox, masked_ok):
    """같은 15초 창의 프레임은 서로 거의 같아 많이 남겨도 소용없다."""
    out = DC.collect("CAM-A", "가지점", frames(n=30))
    assert out["saved"] == DC.MAX_PER_ROUND


def test_프레임이_없으면_건너뛴다(sandbox, masked_ok):
    out = DC.collect("CAM-A", "가지점", [])
    assert out["saved"] == 0 and out["reason"] == DC.SKIP_NO_FRAME


# --- 마스킹 -----------------------------------------------------------------

def test_마스킹에_실패하면_저장하지_않는다(sandbox, monkeypatch):
    """ROI 정지영상과 다르다 — 학습 데이터는 오래 보관하는 파일이다."""
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.FAILED, 0))
    out = DC.collect("CAM-A", "가지점", frames())
    assert out["saved"] == 0 and out["reason"] == DC.SKIP_MASK
    assert not list((sandbox / "CAM-A").glob("*.jpg"))


def test_검출기가_없어도_저장하지_않는다(sandbox, monkeypatch):
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.UNAVAILABLE, 0))
    assert DC.collect("CAM-A", "가지점", frames())["saved"] == 0


def test_사람을_가린_프레임은_저장한다(sandbox, monkeypatch):
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.MASKED, 3))
    DC.collect("CAM-A", "가지점", frames())
    rec = json.loads((sandbox / "CAM-A" / DC.META_NAME)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert rec["masked_people"] == 3


# --- 상한 -------------------------------------------------------------------

def test_최소_간격_안에는_다시_담지_않는다(sandbox, masked_ok):
    """집중 감시(60초 주기)가 같은 장면을 연달아 쌓지 않게 한다."""
    assert DC.collect("CAM-A", "가지점", frames())["saved"] > 0
    out = DC.collect("CAM-A", "가지점", frames())
    assert out["saved"] == 0 and out["reason"] == DC.SKIP_INTERVAL


def test_지점당_하루_상한을_지킨다(sandbox, masked_ok, monkeypatch):
    monkeypatch.setattr(DC, "MIN_INTERVAL_SEC", 0.0)
    for _ in range(20):
        DC.collect("CAM-A", "가지점", frames())
    saved = len(list((sandbox / "CAM-A").glob("*.jpg")))
    assert saved <= DC.MAX_PER_DAY
    assert DC.collect("CAM-A", "가지점", frames())["reason"] == DC.SKIP_DAILY


def test_상한은_지점마다_따로_센다(sandbox, masked_ok, monkeypatch):
    monkeypatch.setattr(DC, "MIN_INTERVAL_SEC", 0.0)
    for _ in range(20):
        DC.collect("CAM-A", "가지점", frames())
    assert DC.collect("CAM-B", "나지점", frames())["saved"] > 0


def test_디스크_상한을_넘으면_멈춘다(sandbox, masked_ok, monkeypatch):
    """조용히 디스크를 채우고 서비스가 함께 죽는 것이 가장 나쁘다."""
    monkeypatch.setattr(DC, "MAX_TOTAL_MB", 0.0)
    out = DC.collect("CAM-A", "가지점", frames())
    assert out["saved"] == 0 and out["reason"] == DC.SKIP_DISK


# --- 견고성 -----------------------------------------------------------------

def test_저장이_터져도_예외를_올리지_않는다(sandbox, monkeypatch):
    """수집 때문에 관제 분석이 멈추면 안 된다."""
    def boom(*a, **kw):
        raise RuntimeError("디스크 오류")
    monkeypatch.setattr(DC, "_save", boom)
    out = DC.collect("CAM-A", "가지점", frames())
    assert out["saved"] == 0 and out["reason"] == DC.SKIP_ERROR


def test_sink_은_분석기_콜백으로_쓸_수_있다(sandbox, masked_ok):
    DC.sink("CAM-A", "가지점")(frames())
    assert list((sandbox / "CAM-A").glob("*.jpg"))


def test_현황에_누적_장수와_상한이_담긴다(sandbox, masked_ok):
    DC.collect("CAM-A", "가지점", frames())
    st = DC.status()
    assert st["frames"] == DC.MAX_PER_ROUND
    assert st["enabled"] is True
    assert st["per_day"] == DC.MAX_PER_DAY
    assert st["max_mb"] == DC.MAX_TOTAL_MB


# --- 사용자 영상에서 프레임 뽑기 (2026-08-12) --------------------------------
# 등록된 부산 CCTV는 대부분 320~352×240이라 사람이 손상 박스를 그을 수 없다.
# 현장 촬영·블랙박스 영상이 있으면 그쪽이 훨씬 쓸 만하므로 직접 넣을 통로를 둔다.

def _make_video(path, frames=60, w=160, h=120, fps=10.0):
    """프레임마다 밝기가 다른 시험용 영상. 뽑힌 장면을 구분할 수 있다."""
    import cv2
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert vw.isOpened(), "시험용 영상을 만들지 못했습니다"
    for i in range(frames):
        vw.write(np.full((h, w, 3), (i * 4) % 256, dtype=np.uint8))
    vw.release()
    return path


def test_영상에서_프레임을_뽑아_저장한다(sandbox, masked_ok, tmp_path):
    v = _make_video(tmp_path / "road.mp4", frames=60, fps=10.0)
    res = DC.ingest_video(v, label="현장 순찰 영상", interval_sec=1.0, max_frames=5)
    assert res["ok"] is True and res["saved"] == 5
    out = pathlib.Path(res["dir"])
    assert len(list(out.glob("*.jpg"))) == 5


def test_영상_전체에_고르게_퍼뜨린다(sandbox, masked_ok, tmp_path):
    """앞부분만 뽑으면 촬영 초반 몇 분만 학습하게 된다."""
    v = _make_video(tmp_path / "road.mp4", frames=100, fps=10.0)
    res = DC.ingest_video(v, label="테스트", interval_sec=0.1, max_frames=4)
    meta = _meta(res)
    pos = [m["video_position_sec"] for m in meta]
    assert pos == sorted(pos)
    # 100프레임/10fps = 10초짜리인데 4장이면 마지막 표본이 후반부여야 한다.
    assert pos[-1] >= 5.0, f"뒤쪽까지 훑지 않았다: {pos}"


def test_뽑힌_프레임은_서로_다르다(sandbox, masked_ok, tmp_path):
    """탐색이 먹히지 않는데 먹힌 줄 알면 같은 장면만 반복해 뽑는다."""
    import hashlib
    v = _make_video(tmp_path / "road.mp4", frames=100, fps=10.0)
    res = DC.ingest_video(v, label="테스트", interval_sec=0.5, max_frames=5)
    out = pathlib.Path(res["dir"])
    digests = {hashlib.md5(p.read_bytes()).hexdigest()
               for p in sorted(out.glob("*.jpg"))}
    assert len(digests) == res["saved"]


def test_출처와_촬영_지점을_메타에_남긴다(sandbox, masked_ok, tmp_path):
    v = _make_video(tmp_path / "road.mp4", frames=40, fps=10.0)
    res = DC.ingest_video(v, label="2026-08 광안대로 순찰", interval_sec=1.0,
                          max_frames=3)
    m = _meta(res)[0]
    assert m["video_label"] == "2026-08 광안대로 순찰"
    assert m["source"] == "upload"
    assert m["video_position_sec"] == 0.0
    assert m["plate_masked"] is False       # 번호판은 가리지 못한다


def test_폴더_이름은_ASCII다(sandbox, masked_ok, tmp_path):
    """라벨링 도구가 cv2.imread 를 쓰는데, OpenCV 는 Windows 에서 한글이 섞인
    경로를 읽지 못하고 조용히 None 을 돌려준다."""
    v = _make_video(tmp_path / "road.mp4", frames=20, fps=10.0)
    res = DC.ingest_video(v, label="한글 이름 영상", interval_sec=1.0, max_frames=2)
    assert res["dir_id"].isascii()
    assert pathlib.Path(res["dir"]).name.isascii()


def test_마스킹하지_못한_프레임은_저장하지_않는다(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.FAILED, 0))
    v = _make_video(tmp_path / "road.mp4", frames=30, fps=10.0)
    res = DC.ingest_video(v, label="테스트", interval_sec=1.0, max_frames=3)
    assert res["ok"] is False and res["saved"] == 0
    assert res["mask_skipped"] > 0
    assert "가리지" in res["error"]


def test_열_수_없는_파일은_사유를_돌려준다(sandbox, masked_ok, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    res = DC.ingest_video(bad, label="테스트")
    assert res["ok"] is False and "열지 못했습니다" in res["error"]


def test_디스크_상한을_넘으면_시작하지_않는다(sandbox, masked_ok, tmp_path,
                                             monkeypatch):
    monkeypatch.setattr(DC, "MAX_TOTAL_MB", 0.0)
    v = _make_video(tmp_path / "road.mp4", frames=20, fps=10.0)
    res = DC.ingest_video(v, label="테스트")
    assert res["ok"] is False and res["reason"] == DC.SKIP_DISK


def test_자동_수집이_꺼져_있어도_업로드는_동작한다(tmp_path, monkeypatch,
                                                  masked_ok):
    """사람이 파일을 골라 올리는 명시적 행위라 저절로 쌓이지 않는다.
    자동 수집 스위치와 묶으면 「왜 안 되지」로 헷갈린다."""
    monkeypatch.setattr(DC, "DATASET_DIR", tmp_path / "raw")
    monkeypatch.setattr(DC, "is_enabled", lambda: False)
    DC.reset_state()
    v = _make_video(tmp_path / "road.mp4", frames=20, fps=10.0)
    assert DC.ingest_video(v, label="테스트", max_frames=2)["ok"] is True


def test_최소_간격_상한은_업로드에_적용되지_않는다(sandbox, masked_ok, tmp_path):
    """자동 수집의 지점당 하루·최소 간격은 순회용이다. 연달아 올릴 수 있어야 한다."""
    v = _make_video(tmp_path / "road.mp4", frames=20, fps=10.0)
    first = DC.ingest_video(v, label="1차", max_frames=2)
    second = DC.ingest_video(v, label="2차", max_frames=2)
    assert first["ok"] and second["ok"]
