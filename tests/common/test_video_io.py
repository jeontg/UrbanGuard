from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import iter_source, probe_video, read_first_frame

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
