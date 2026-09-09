"""동영상 업로드(core/video_store.py)와 ROI용 정지영상(core/snapshot.py).

ROI 설정이 **탐지 파이프라인 가동에 의존하지 않는가**가 핵심이다.
동영상을 새로 올리면 바로 ROI를 그릴 수 있어야 한다.
"""
from __future__ import annotations

import asyncio

import pytest

from tot_dashboard.core import snapshot as SNAP
from tot_dashboard.core import video_store as V


def _make_video(path, frames=30, size=(160, 120)):
    """시험용 mp4 한 편. 코덱이 없으면 건너뛴다."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    w, h = size
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (w, h))
    if not vw.isOpened():
        pytest.skip("mp4v 코덱을 쓸 수 없는 환경")
    for i in range(frames):
        img = np.full((h, w, 3), (i * 8) % 256, np.uint8)
        vw.write(img)
    vw.release()
    return path


class _Upload:
    """FastAPI UploadFile 흉내 — read(size) 로 조각을 준다."""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._buf = data
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._buf) - self._pos
        chunk = self._buf[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


@pytest.fixture
def vdir(tmp_path, monkeypatch):
    # 실제 규칙과 같은 배치를 만든다 — VIDEO_DIR 은 PROJECT_ROOT/data/videos 이고
    # DB에 남는 상대경로(REL_PREFIX)가 그 위치를 가리켜야 한다.
    d = tmp_path / "data" / "videos"
    monkeypatch.setattr(V, "VIDEO_DIR", d)
    monkeypatch.setattr(SNAP, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(V, "PROJECT_ROOT", tmp_path)
    return d


def _save(upload, cam_id="CAM-V1"):
    """받아서 곧바로 확정 — 정상 흐름(등록 성공)에 해당한다."""
    staged = asyncio.run(V.stage_upload(upload, cam_id))
    return V.finalize(staged, cam_id)


# --- 업로드 검증 -------------------------------------------------------------
def test_동영상이_아닌_확장자는_거른다(vdir):
    with pytest.raises(V.VideoError, match="동영상 파일만"):
        _save(_Upload("bad.exe", b"MZ..."))


def test_빈_파일은_거른다(vdir):
    with pytest.raises(V.VideoError, match="빈 파일"):
        _save(_Upload("empty.mp4", b""))


def test_상한을_넘으면_거르고_잘린_파일을_남기지_않는다(vdir, monkeypatch):
    monkeypatch.setattr(V, "MAX_BYTES", 100)
    monkeypatch.setattr(V, "CHUNK", 32)
    with pytest.raises(V.VideoError, match="너무 큽니다"):
        _save(_Upload("big.mp4", b"x" * 500))
    assert list(vdir.glob("*")) == []


def test_확장자만_동영상인_가짜_파일을_거른다(vdir):
    """열리지 않는 파일을 등록하면 ROI 화면이 빈 채로 남는다."""
    with pytest.raises(V.VideoError, match="열지 못했습니다"):
        _save(_Upload("fake.mp4", b"not really a video" * 50))
    assert list(vdir.glob("*")) == []


def test_저장하면_프로젝트_기준_상대경로를_돌려준다(vdir, tmp_path):
    src = _make_video(tmp_path / "src.mp4")
    rel = _save(_Upload("도로 침수 영상.mp4", src.read_bytes()))
    assert rel.startswith("data/videos/")
    assert (vdir / rel.split("/")[-1]).is_file()


def test_한글_파일명이어도_카메라_ID로_알아볼_수_있다(vdir, tmp_path):
    src = _make_video(tmp_path / "src.mp4")
    rel = _save(_Upload("옥천군 침수.mp4", src.read_bytes()), "CAM-OKCHEON")
    assert "CAM-OKCHEON" in rel


def test_다시_올리면_이전_영상을_남기지_않는다(vdir, tmp_path):
    """한 카메라는 영상 하나만 가리킨다 — 쌓이면 디스크만 먹는다."""
    src = _make_video(tmp_path / "src.mp4")
    _save(_Upload("first.mp4", src.read_bytes()), "CAM-X")
    _save(_Upload("second.mp4", src.read_bytes()), "CAM-X")
    assert len(list(vdir.glob("CAM-X__*"))) == 1


def test_다른_카메라의_영상은_건드리지_않는다(vdir, tmp_path):
    src = _make_video(tmp_path / "src.mp4")
    _save(_Upload("a.mp4", src.read_bytes()), "CAM-A")
    _save(_Upload("b.mp4", src.read_bytes()), "CAM-B")
    assert len(list(vdir.glob("CAM-A__*"))) == 1
    assert len(list(vdir.glob("CAM-B__*"))) == 1


# --- 정지영상 ---------------------------------------------------------------
class _Cam:
    def __init__(self, **kw):
        self.id = kw.get("id", "CAM-1")
        self.source_type = kw.get("source_type", "video")
        self.source_path = kw.get("source_path", "")
        self.source_url = kw.get("source_url", "")


def test_동영상에서_정지영상을_뽑는다(vdir, tmp_path, monkeypatch):
    """파이프라인이 돌지 않아도 ROI를 그릴 수 있어야 한다."""
    monkeypatch.setattr("tot_dashboard.core.image_mask.mask_array",
                        lambda img, **k: ("no_target", 0))
    src = _make_video(tmp_path / "clip.mp4")
    b64, mask, err = SNAP.grab(_Cam(source_path=str(src)))
    assert err == ""
    assert b64            # base64 JPEG
    assert mask == "no_target"


def test_동영상_경로가_없으면_사유를_알려준다(vdir):
    b64, _mask, err = SNAP.grab(_Cam(source_path="data/videos/없는파일.mp4"))
    assert b64 == ""
    assert "찾을 수 없습니다" in err


def test_합성_소스는_정지영상이_없다고_알려준다(vdir):
    b64, _mask, err = SNAP.grab(_Cam(source_type="synthetic"))
    assert b64 == ""
    assert "합성" in err


def test_스트림_주소가_비어_있으면_사유를_알려준다(vdir):
    b64, _mask, err = SNAP.grab(_Cam(source_type="hls", source_url=""))
    assert b64 == ""
    assert "스트림 주소가 없습니다" in err


def test_마스킹이_실패해도_화면은_내보낸다(vdir, tmp_path, monkeypatch):
    """인파 관측 스냅샷과 다르다 — 영상이 없으면 ROI를 그릴 수가 없다.
    대신 가리지 못한 사실을 상태로 돌려주고 화면이 경고를 띄운다."""
    monkeypatch.setattr("tot_dashboard.core.image_mask.mask_array",
                        lambda img, **k: ("unavailable", 0))
    src = _make_video(tmp_path / "clip.mp4")
    b64, mask, err = SNAP.grab(_Cam(source_path=str(src)))
    assert b64 and err == ""
    assert mask == "unavailable"


def test_동영상_정보를_읽어_화면에_보여줄_수_있다(vdir, tmp_path):
    src = _make_video(tmp_path / "clip.mp4", frames=30)
    rel = _save(_Upload("clip.mp4", src.read_bytes()), "CAM-INFO")
    got = V.info(rel)
    assert got["exists"] is True
    assert got["width"] > 0 and got["height"] > 0


def test_등록이_실패하면_임시파일만_지우고_이전_영상은_지키다(vdir, tmp_path):
    """검증에서 걸렸는데 이전 영상이 사라지면, 멀쩡히 돌던 지점이 멈춘다.
    실제로 겪은 결함이라 회귀 시험으로 남긴다(2026-08-08)."""
    src = _make_video(tmp_path / "src.mp4")
    keep = _save(_Upload("old.mp4", src.read_bytes()), "CAM-KEEP")

    # 새 파일을 받아 두고 등록이 실패한 상황
    staged = asyncio.run(V.stage_upload(_Upload("new.mp4", src.read_bytes()),
                                        "CAM-KEEP"))
    V.discard(staged)

    assert V.info(keep)["exists"] is True, "이전 영상이 사라졌다"
    assert list(vdir.glob("*.uploading")) == [], "임시 파일이 남았다"


def test_확정_전에는_최종_파일이_생기지_않는다(vdir, tmp_path):
    src = _make_video(tmp_path / "src.mp4")
    staged = asyncio.run(V.stage_upload(_Upload("x.mp4", src.read_bytes()), "CAM-S"))
    assert staged.name.endswith(".uploading")
    assert list(vdir.glob("CAM-S__*.mp4")) == []
    rel = V.finalize(staged, "CAM-S")
    assert V.info(rel)["exists"] is True
