"""Phase 3 - 자체 CCTV 프레임에 포트홀/균열 바운딩박스를 직접 라벨링하는 도구.

``scripts/roi_editor.py``와 동일한 클릭 기반 OpenCV GUI 패턴을 따른다. 이
스크립트는 사람이 직접 눈으로 보고 판단해서 박스를 그리는 용도이며, 라벨의
정확성은 전적으로 사용자의 판단에 달려 있다 - 자동으로 손상을 찾아주지
않는다(그건 아직 정확도가 검증되지 않은 road/defect_detection.py의 역할이며,
Phase 2에서 확인했듯 지금은 신뢰할 수 없다).

폴더 하나(``scripts/collect_road_cctv_frames.py``로 모은
``data/datasets/road_cctv_own/raw/<block_id>/``)를 대상으로, 안에 있는 이미지를
한 장씩 넘기며 라벨링한다. 결과는 YOLO 포맷으로
``data/datasets/road_cctv_own/labeled/<block_id>/images/`` +
``.../labels/``에 저장된다(이미지 복사 + 같은 이름의 .txt).

Usage:
    python scripts/label_road_defects.py --block BLOCK-OLYMPIC
    python scripts/label_road_defects.py --folder data/datasets/road_cctv_own/raw/BLOCK-OLYMPIC

Controls:
  1          클래스를 포트홀로 설정(기본값)
  2          클래스를 균열로 설정
  좌클릭+드래그   박스 그리기(그리는 동안 현재 클래스 색으로 미리보기)
  우클릭      가장 최근에 그린 박스 취소
  n / SPACE  이 이미지 라벨 저장하고 다음 이미지로
  s          현재까지 라벨(빈 이미지 포함)을 저장만 하고 다음으로(= n과 동일)
  b          이전 이미지로 돌아가기(그 이미지의 기존 라벨을 불러와 이어서 편집)
  q / ESC    라벨링 종료(지금까지 저장된 것은 유지됨)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import list_folder_images

CLASS_NAMES = {0: "pothole", 1: "crack"}
CLASS_COLOR = {0: (0, 0, 229), 1: (23, 168, 212)}  # BGR, road/defect_detection.py의 색과 동일


def _yolo_line(cls_id: int, box: tuple[int, int, int, int], w: int, h: int) -> str:
    x1, y1, x2, y2 = box
    xc, yc = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
    bw, bh = abs(x2 - x1) / w, abs(y2 - y1) / h
    return f"{cls_id} {xc:.6f} {yc:.6f} {bw:.6f} {bh:.6f}"


def _load_existing_labels(label_path: Path, w: int, h: int) -> list[tuple[int, tuple[int, int, int, int]]]:
    if not label_path.exists():
        return []
    boxes = []
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        cls_id = int(parts[0])
        xc, yc, bw, bh = (float(v) for v in parts[1:])
        x1 = int((xc - bw / 2) * w)
        y1 = int((yc - bh / 2) * h)
        x2 = int((xc + bw / 2) * w)
        y2 = int((yc + bh / 2) * h)
        boxes.append((cls_id, (x1, y1, x2, y2)))
    return boxes


class _Labeler:
    def __init__(self, image_paths: list[Path], out_images: Path, out_labels: Path):
        self.image_paths = image_paths
        self.out_images = out_images
        self.out_labels = out_labels
        self.idx = 0
        self.cls_id = 0
        self.boxes: list[tuple[int, tuple[int, int, int, int]]] = []
        self.drawing = False
        self.start_pt = (0, 0)
        self.cur_pt = (0, 0)
        self.win = ("Road defect labeler -- 1/2:class  drag:box  right-click:undo  "
                    "n/space:save+next  b:back  q:quit")
        cv2.namedWindow(self.win)
        cv2.setMouseCallback(self.win, self._on_mouse)

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drawing = True
            self.start_pt = (x, y)
            self.cur_pt = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self.drawing:
            self.cur_pt = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self.drawing:
            self.drawing = False
            box = (self.start_pt[0], self.start_pt[1], x, y)
            if abs(box[2] - box[0]) >= 4 and abs(box[3] - box[1]) >= 4:
                self.boxes.append((self.cls_id, box))
        elif event == cv2.EVENT_RBUTTONDOWN and self.boxes:
            self.boxes.pop()

    def _draw(self, frame):
        img = frame.copy()
        for cls_id, (x1, y1, x2, y2) in self.boxes:
            color = CLASS_COLOR.get(cls_id, (0, 200, 0))
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, CLASS_NAMES.get(cls_id, str(cls_id)), (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        if self.drawing:
            color = CLASS_COLOR.get(self.cls_id, (0, 200, 0))
            cv2.rectangle(img, self.start_pt, self.cur_pt, color, 1)
        label = (f"[{self.idx + 1}/{len(self.image_paths)}] {self.image_paths[self.idx].name}  "
                 f"class={CLASS_NAMES.get(self.cls_id)}(1/2 to change)  boxes={len(self.boxes)}")
        cv2.rectangle(img, (0, 0), (img.shape[1], 26), (0, 0, 0), -1)
        cv2.putText(img, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
        return img

    def _save_current(self, frame_shape) -> None:
        h, w = frame_shape[:2]
        img_path = self.image_paths[self.idx]
        label_path = self.out_labels / f"{img_path.stem}.txt"
        lines = [_yolo_line(cid, box, w, h) for cid, box in self.boxes]
        label_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        out_img_path = self.out_images / img_path.name
        if not out_img_path.exists():
            cv2.imwrite(str(out_img_path), cv2.imread(str(img_path)))

    def run(self) -> None:
        while 0 <= self.idx < len(self.image_paths):
            img_path = self.image_paths[self.idx]
            frame = cv2.imread(str(img_path))
            if frame is None:
                print(f"[label_road_defects] 이미지를 읽을 수 없음, 건너뜀: {img_path}")
                self.idx += 1
                continue
            h, w = frame.shape[:2]
            existing_label = self.out_labels / f"{img_path.stem}.txt"
            self.boxes = _load_existing_labels(existing_label, w, h)

            advance = None
            while advance is None:
                cv2.imshow(self.win, self._draw(frame))
                key = cv2.waitKey(30) & 0xFF
                if key == ord("1"):
                    self.cls_id = 0
                elif key == ord("2"):
                    self.cls_id = 1
                elif key in (ord("n"), ord(" "), ord("s")):
                    self._save_current(frame.shape)
                    advance = 1
                elif key == ord("b"):
                    advance = -1
                elif key in (27, ord("q")):
                    cv2.destroyAllWindows()
                    print(f"[label_road_defects] 종료 -- {self.out_labels}까지 저장된 라벨은 유지됩니다.")
                    return
            self.idx += advance
        cv2.destroyAllWindows()
        print(f"[label_road_defects] 모든 이미지를 다 봤습니다. 결과: {self.out_images} / {self.out_labels}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--block", default=None,
                    help="data/datasets/road_cctv_own/raw/<block>의 이미지를 라벨링")
    ap.add_argument("--folder", default=None, help="임의의 이미지 폴더를 직접 지정")
    args = ap.parse_args()

    if args.folder:
        in_dir = Path(args.folder)
        block_id = in_dir.name
    elif args.block:
        in_dir = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "raw" / args.block
        block_id = args.block
    else:
        raise SystemExit("--block 또는 --folder 중 하나가 필요합니다")

    images = list_folder_images(in_dir)
    if not images:
        raise SystemExit(f"라벨링할 이미지가 없습니다: {in_dir} "
                          "(먼저 scripts/collect_road_cctv_frames.py로 프레임을 모으세요)")

    out_root = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "labeled" / block_id
    out_images = out_root / "images"
    out_labels = out_root / "labels"
    out_images.mkdir(parents=True, exist_ok=True)
    out_labels.mkdir(parents=True, exist_ok=True)

    print(f"[label_road_defects] {len(images)}장 라벨링 시작 -- {in_dir}")
    _Labeler(images, out_images, out_labels).run()


if __name__ == "__main__":
    main()
