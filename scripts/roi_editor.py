"""Road-ROI calibration tool -- click-based, one camera/video at a time.

Ported and generalized from flood3's ``scripts/roi_editor.py``. The original
was hard-wired to ``configs/blocks.json`` block ids + a live HLS stream (with
a fallback to the most recent image saved by flood3's standalone
``cctv_capture`` tool, which was not ported into this repo -- see
docs/integration_plan.md section 5-3/README "Known gaps"). This version works
against any of three frame sources, so it also covers ad hoc videos that
aren't one of the configured blocks (e.g. a sample clip copied in from
another project):

  --video <path>   grab the first frame of a local video file
  --frame <path>    use a still image directly
  --block <id>      original flood3 workflow: look up configs/blocks.json,
                    grab a frame from its live HLS/RTSP source

Draws road ROI -> low-point ROI (optional) -> lane threshold line (optional),
saving a ``common.roi.RoiConfig`` JSON. Road/low-point ROI can each be made of
several polygon pieces (an intersection's approach roads are often visually
separated) -- finish a piece with ENTER to start the next one; press ENTER
again on an empty piece to move to the next stage.

Usage (needs a local display -- not runnable in a headless/CI environment):
    python scripts/roi_editor.py --video data/samples/flood/my_video.mp4 --camera-name my-camera
    python scripts/roi_editor.py --frame path/to/image.jpg --camera-name my-camera
    python scripts/roi_editor.py --block BLOCK-CHORYANG

Controls:
  left click       add a point to the current piece
  right click      undo the last point
  ENTER            >=3 points: commit the current piece, start a new one (same stage)
                    0 points: advance to the next stage
  s                save immediately with everything committed so far
                    (+ the current piece, auto-included if it has >=3 points)
  q / ESC          quit without saving
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.roi import RoiConfig, save_roi_config
from tot_dashboard.common.video_io import read_first_frame


def _load_blocks() -> list[dict]:
    path = PROJECT_ROOT / "configs" / "blocks.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)["blocks"]


def _grab_hls_frame(url: str, timeout_tries: int = 30):
    cap = cv2.VideoCapture(url)
    frame = None
    if cap.isOpened():
        for _ in range(timeout_tries):
            ok, f = cap.read()
            if ok:
                frame = f
                break
        cap.release()
    return frame


def _grab_frame(args) -> tuple:
    """Returns (frame, camera_name, default_out_path)."""
    if args.frame:
        img = cv2.imread(args.frame)
        if img is None:
            raise SystemExit(f"cannot read frame image: {args.frame}")
        name = args.camera_name or Path(args.frame).stem
        return img, name, PROJECT_ROOT / "configs" / "roi" / f"{name}.json"

    if args.video:
        img = read_first_frame(args.video)
        if img is None:
            raise SystemExit(f"cannot read first frame of video: {args.video}")
        name = args.camera_name or Path(args.video).stem
        return img, name, PROJECT_ROOT / "configs" / "roi" / f"{name}.json"

    if args.block:
        blocks = _load_blocks()
        block = next((b for b in blocks if b["id"] == args.block), None)
        if block is None:
            raise SystemExit(f"block id not found in configs/blocks.json: {args.block}")
        url = (block.get("source") or {}).get("url")
        if not url:
            raise SystemExit(
                f"block {args.block} has no source.url (synthetic block?) -- "
                "use --frame or --video instead"
            )
        print(f"[roi_editor] grabbing frame from HLS: {url}")
        img = _grab_hls_frame(url)
        if img is None:
            raise SystemExit(
                f"could not connect to {url}. Pass --frame with a locally saved "
                "still image instead (this port does not include flood3's "
                "periodic-capture-to-disk fallback -- see README's Known gaps)."
            )
        return img, block["id"], PROJECT_ROOT / "configs" / "roi" / f"{block['id']}.json"

    raise SystemExit("one of --video, --frame, or --block is required")


class _Editor:
    """Canvas for clicking several polygon pieces in sequence. One stage = one
    list of pieces."""

    def __init__(self, frame):
        self.frame = frame
        self.points: list[list[int]] = []  # piece currently being drawn
        self.win = "ROI editor -- left:add point  right:undo  ENTER:commit/next  s:save  q:quit"
        cv2.namedWindow(self.win)
        cv2.setMouseCallback(self.win, self._on_mouse)

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.points.append([x, y])
        elif event == cv2.EVENT_RBUTTONDOWN and self.points:
            self.points.pop()

    def _draw(self, label: str, committed: list[list[list[int]]],
              extra_polys: list[tuple[list, tuple]]):
        img = self.frame.copy()
        for poly, color in extra_polys:  # reference polygons from earlier stages
            if len(poly) >= 2:
                cv2.polylines(img, [self._np(poly)], len(poly) >= 3, color, 2)
        for poly in committed:  # pieces committed this stage (green)
            cv2.polylines(img, [self._np(poly)], True, (0, 200, 0), 2)
        if self.points:  # piece currently being drawn (yellow)
            cv2.polylines(img, [self._np(self.points)], False, (0, 255, 255), 2)
            for p in self.points:
                cv2.circle(img, tuple(p), 4, (0, 255, 255), -1)
        cv2.putText(img, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(img, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 0, 0), 1, cv2.LINE_AA)
        return img

    @staticmethod
    def _np(points):
        return np.asarray(points, dtype=int).reshape(-1, 1, 2)

    def run_stage_multi(self, label: str, extra_polys: list[tuple[list, tuple]],
                        min_polys: int = 0):
        """One stage of several polygon pieces.

        Returns (committed_polygons, cancelled, save_now). If cancelled,
        committed should be discarded.
        """
        committed: list[list[list[int]]] = []
        self.points = []
        while True:
            tag = f"{label} -- {len(committed)} piece(s) committed"
            cv2.imshow(self.win, self._draw(tag, committed, extra_polys))
            key = cv2.waitKey(30) & 0xFF
            if key in (13, 10):  # ENTER
                if len(self.points) >= 3:
                    committed.append(list(self.points))
                    self.points = []
                    continue
                if len(self.points) == 0:
                    if len(committed) >= min_polys:
                        return committed, False, False
                    print(f"[roi_editor] need at least {min_polys} piece(s) "
                          f"(have {len(committed)})")
                    continue
                print(f"[roi_editor] need at least 3 points (have {len(self.points)}) "
                      "-- keep clicking, or right-click to undo")
            elif key == ord("s"):
                if len(self.points) >= 3:
                    committed.append(list(self.points))
                return committed, False, True
            elif key in (27, ord("q")):
                return committed, True, False

    def run_stage_line(self, label: str, extra_polys: list[tuple[list, tuple]]):
        """Lane threshold line only -- exactly 2 points or 0 (skip), not a polygon."""
        self.points = []
        while True:
            cv2.imshow(self.win, self._draw(label, [], extra_polys))
            key = cv2.waitKey(30) & 0xFF
            if key in (13, 10):
                if len(self.points) == 0:
                    return None
                if len(self.points) == 2:
                    return list(self.points)
                print(f"[roi_editor] the lane line needs exactly 2 points "
                      f"(have {len(self.points)}) -- right-click to undo and retry")
            elif key == ord("s"):
                return list(self.points) if len(self.points) == 2 else None
            elif key in (27, ord("q")):
                return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", default=None, help="grab the first frame of a local video file")
    ap.add_argument("--frame", default=None, help="use a local still image directly")
    ap.add_argument("--block", default=None,
                    help="configs/blocks.json block id (original flood3 workflow, live HLS)")
    ap.add_argument("--camera-name", default=None,
                    help="RoiConfig.camera_name and output filename stem "
                         "(default: derived from --video/--frame filename, or the block id)")
    ap.add_argument("--out", default=None,
                    help="output path (default: configs/roi/<camera-name>.json)")
    args = ap.parse_args()

    frame, camera_name, default_out = _grab_frame(args)
    h, w = frame.shape[:2]
    print(f"[roi_editor] frame size: {w}x{h}")

    out_path = Path(args.out) if args.out else default_out
    editor = _Editor(frame)
    cfg = RoiConfig(camera_name=camera_name, frame_width=w, frame_height=h)

    def _flatten(polys: list[list[list[int]]]) -> list[tuple[list, tuple]]:
        return [(p, (0, 200, 255)) for p in polys]

    # 1) Road ROI (several pieces allowed; 0 pieces -> whole frame treated as road)
    road, cancelled, save_now = editor.run_stage_multi(
        "1/3 road ROI (multiple pieces ok) -- ENTER:add piece/next, s:save, q:cancel",
        [], min_polys=0)
    if cancelled:
        print("[roi_editor] cancelled (not saved)")
        cv2.destroyAllWindows()
        return
    cfg.road_roi = road
    if save_now:
        save_roi_config(cfg, out_path)
        print(f"[roi_editor] saved: {out_path} (road_roi: {len(road)} piece(s))")
        cv2.destroyAllWindows()
        return

    # 2) Low-point ROI (optional, several pieces). q/ESC always cancels everything.
    low, cancelled, save_now = editor.run_stage_multi(
        "2/3 low-point ROI (optional, ENTER to skip) -- ENTER:add piece/next, s:save, q:cancel",
        _flatten(road), min_polys=0)
    if cancelled:
        print("[roi_editor] cancelled (not saved)")
        cv2.destroyAllWindows()
        return
    cfg.low_point_roi = low
    if save_now:
        save_roi_config(cfg, out_path)
        print(f"[roi_editor] saved: {out_path}")
        cv2.destroyAllWindows()
        return

    # 3) Lane threshold line (optional, single 2-point segment -- not a polygon)
    lane = editor.run_stage_line(
        "3/3 lane threshold line (optional, exactly 2 points or ENTER to skip) -- s:save",
        _flatten(road) + _flatten(low))
    if lane:
        cfg.lane_threshold_line = lane

    save_roi_config(cfg, out_path)
    print(f"[roi_editor] saved: {out_path}")
    print(f"  road_roi={len(cfg.road_roi)} piece(s)  low_point_roi={len(cfg.low_point_roi)} piece(s)  "
          f"lane_threshold_line={'yes' if cfg.lane_threshold_line else 'no'}")
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
