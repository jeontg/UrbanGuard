"""ffmpeg-based image/video capture from an HLS/RTSP/http CCTV stream.

Ported from flood3's ``cctv_capture/capture.py`` — only ``capture_image``/
``capture_video`` (used by the service layer's ``/api/record/{block_id}``
endpoint, Phase 7). The original file's standalone CLI/manifest tooling for
periodically capturing all configured blocks to disk
(``capture_all``/``read_manifest``/the ``__main__`` loop) is a separate,
already-decoupled concern (its own FastAPI viewer app in flood3) and is not
ported in this phase — see docs/integration_plan.md section 5-3, which notes
``cctv_capture/`` can be lifted wholesale later if needed.
"""
from __future__ import annotations

import os
import subprocess

USER_AGENT = "Mozilla/5.0 (tot-dashboard-cctv-capture)"


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        from shutil import which
        return which("ffmpeg") or "ffmpeg"


FFMPEG = ffmpeg_exe()


def _run(args: list[str], timeout: float) -> tuple[bool, str]:
    try:
        p = subprocess.run([FFMPEG, *args], capture_output=True, timeout=timeout)
        return p.returncode == 0, p.stderr.decode("utf-8", "ignore")[-300:]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def _in(url: str) -> list[str]:
    a = ["-y", "-loglevel", "error"]
    if str(url).lower().startswith(("http", "rtsp", "rtmp")):  # UA only for network inputs
        a += ["-user_agent", USER_AGENT]
    return a + ["-i", url]


def _nonempty(path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > 0


def capture_image(url: str, out_path: str, timeout: float = 40) -> tuple[bool, str]:
    ok, err = _run(_in(url) + ["-frames:v", "1", "-q:v", "2", out_path], timeout)
    return (ok and _nonempty(out_path)), err


def capture_video(url: str, out_path: str, dur: int = 10,
                  timeout: float | None = None) -> tuple[bool, str]:
    timeout = timeout or (dur + 45)
    # 1) stream copy: original H.264 as-is -> browser-playable, fast
    ok, err = _run(_in(url) + ["-t", str(dur), "-an", "-c:v", "copy",
                               "-movflags", "+faststart", out_path], timeout)
    if ok and _nonempty(out_path):
        return True, "copy"
    # 2) re-encode fallback (H.264)
    ok, err = _run(_in(url) + ["-t", str(dur), "-an", "-c:v", "libx264",
                               "-preset", "veryfast", "-pix_fmt", "yuv420p",
                               "-movflags", "+faststart", out_path], timeout)
    return (ok and _nonempty(out_path)), err
