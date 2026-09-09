import cv2
import numpy as np

from tot_dashboard.common import video_io
from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import (ascii_stem, imread_unicode,
                                           imwrite_unicode, iter_source,
                                           patch_cv2_imread_for_unicode_paths,
                                           probe_video, read_first_frame)

SAMPLE_VIDEO = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"


def test_probe_video():
    info = probe_video(SAMPLE_VIDEO)
    assert info.width > 0 and info.height > 0
    assert info.frame_count > 0


def test_read_first_frame():
    frame = read_first_frame(SAMPLE_VIDEO)
    assert frame is not None
    assert frame.ndim == 3


def test_iter_source_samples_at_requested_interval():
    source = {"type": "video", "path": str(SAMPLE_VIDEO)}
    frames = list(iter_source(source, process_every_seconds=1.0))
    assert len(frames) > 0
    frame_numbers = [f[0] for f in frames]
    assert frame_numbers == sorted(frame_numbers)


# --- imread_unicode (2026-08-25) --------------------------------------------
#
# 학습 데이터 보관소(D:\dev-PoC_DATA\02_학습데이터_침수\...)의 폴더명 자체가
# 한글이라, cv2.imread 가 Windows에서 예외 없이 None 을 반환하는 문제를
# 실사용 중 발견했다(AI 모델 학습 화면 — 전 이미지가 조용히 검은 화면으로
# 대체되고 있었다). 여기서는 실제 한글 경로를 재현해 검증한다.

def _write_test_image(path):
    img = np.full((8, 8, 3), 127, np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    buf.tofile(str(path))
    return img


def test_imread_unicode는_한글_폴더명_경로를_읽는다(tmp_path):
    # ⚠ cv2.imread 자체가 이 경로에서 실패하는지는 여기서 단정하지 않는다.
    # 원인이 된 실제 결함(2026-08-25 — 학습 데이터 보관소의 한글 폴더명
    # 경로에서 cv2.imread 가 조용히 None 을 반환)은 스크립트 하나만 도는
    # 상태에서 직접 재현했지만, OpenCV 의 Windows ANSI 경로 처리는 그때그때
    # OS 상태(짧은 파일명 캐시 등)에 좌우돼 **같은 프로세스 안에서도 항상
    # 같게 재현되지 않는다** — 전체 회귀(1,600여 건) 안에서 이 시험만
    # 돌리면 재현되고, 단독으로 돌리면 재현되지 않는 것을 실측으로
    # 확인했다. 그래서 이 시험은 "실패를 재현하는지"가 아니라
    # "imread_unicode 가 항상 올바르게 읽는지"만 확인한다 — 그것이 실제
    # 고친 것이고, 원인이 되는 OS 동작 자체는 우리가 제어할 수 없다.
    kr_dir = tmp_path / "학습데이터_침수" / "flood_water_own"
    kr_dir.mkdir(parents=True)
    img_path = kr_dir / "image_1.jpg"
    _write_test_image(img_path)

    result = imread_unicode(img_path)
    assert result is not None
    assert result.shape == (8, 8, 3)


def test_imread_unicode는_없는_파일에_None을_돌려준다(tmp_path):
    assert imread_unicode(tmp_path / "없음.jpg") is None


def test_patch는_cv2_imread_자체를_바꾼다(tmp_path, monkeypatch):
    kr_dir = tmp_path / "한글폴더"
    kr_dir.mkdir()
    img_path = kr_dir / "image_1.jpg"
    _write_test_image(img_path)

    original = cv2.imread
    try:
        patch_cv2_imread_for_unicode_paths()
        assert cv2.imread(str(img_path)) is not None
    finally:
        cv2.imread = original
        # 다음 시험이 "패치 안 된 원본 상태"를 기대할 수 있게, 내부 플래그도
        # 되돌린다 — 안 그러면 이후 patch_cv2_imread_for_unicode_paths() 호출이
        # "이미 패치됨"으로 보고 조용히 아무 일도 하지 않는다.
        video_io._cv2_imread_patched = False


# --- imwrite_unicode/ascii_stem (2026-08-26) ---------------------------------
#
# extract_flood_frames.py 에 있던 것을 세 번째 사본(extract_traffic_incident_
# frames.py)이 생기며 여기로 옮겼다 — imread_unicode 의 저장(write) 쪽 짝.


def test_imwrite_unicode는_한글_폴더에도_저장한다(tmp_path):
    kr_dir = tmp_path / "학습데이터_교통"
    kr_dir.mkdir()
    img = np.full((8, 8, 3), 200, np.uint8)
    out = kr_dir / "frame_1.jpg"
    assert imwrite_unicode(out, img) is True
    assert out.is_file()
    # 되읽어서 실제로 유효한 이미지인지 확인한다.
    assert imread_unicode(out) is not None


def test_ascii_stem은_비ascii_문자를_밑줄로_바꾼다():
    assert ascii_stem("부산CCTV_debris_01") == "CCTV_debris_01"


def test_ascii_stem은_영숫자와_대시_밑줄은_그대로_둔다():
    assert ascii_stem("busan-2026_rain_01") == "busan-2026_rain_01"


def test_ascii_stem은_연속_밑줄을_하나로_정리한다():
    assert ascii_stem("a__b") == "a_b"


def test_ascii_stem_결과가_비면_video로_대체한다():
    assert ascii_stem("한글만있음") == "video"
