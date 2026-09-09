"""침수 물 영역 폴리곤 라벨링 도구 (방안 B — 자체 학습 데이터 구축).

배경: 기존 물 세그멘테이션 모델 ``models/best.pt``의 학습 데이터가 회수
불가로 확정되어(docs/yolo_license_alternatives.md 2-B절), 자체 데이터를 처음부터
구축한다. 물 영역은 포트홀과 달리 크고 뚜렷해 CCTV 해상도에서도 사람이 충분히
판별 가능하므로, 도로 노면(Phase 3)에서 막혔던 것과 달리 라벨링이 실효성 있다.

**저장 포맷: YOLO-seg 폴리곤** (`class x1 y1 x2 y2 ...`, 0~1 정규화)
이 포맷을 고른 이유는 Ultralytics와 RF-DETR-Seg **양쪽 모두 그대로 읽기** 때문이다
(RF-DETR은 `dataset_file="yolo"` 옵션). 즉 지금 라벨링해두면 나중에 어느
프레임워크를 선택하든 데이터를 다시 만들 필요가 없다 — 프레임워크 종속을 피하는
것이 이번 작업의 핵심 목적이므로 데이터도 중립 포맷으로 남긴다.

입력: 이미지 폴더 (``scripts/collect_flood_frames.py`` 또는
      ``scripts/extract_video_frames.py``로 수집)
출력: data/datasets/flood_water_own/labeled/<set>/{images,labels}/

Usage:
    python scripts/label_flood_water.py --folder data/datasets/flood_water_own/raw/<set>
    python scripts/label_flood_water.py --folder <경로> --set-name my_batch

Controls:
  좌클릭       폴리곤 꼭짓점 추가
  우클릭       마지막 꼭짓점 취소
  ENTER       현재 폴리곤 확정(3점 이상) → 새 폴리곤 시작
              빈 상태에서 ENTER → 아무 동작 안 함(실수 방지)
  n / SPACE   저장하고 다음 이미지 (물이 없으면 폴리곤 0개로 저장 = 배경 샘플)
  b           이전 이미지 (기존 라벨을 불러와 이어서 편집)
  d           현재 이미지의 확정 폴리곤 전체 삭제
  q / ESC     종료 (지금까지 저장분은 유지)

라벨링 지침:
  - 물에 잠긴 노면 영역의 **경계**를 따라 찍는다. 반사광·젖은 노면은
    물이 고여 있지 않으면 제외(단순히 젖은 것과 잠긴 것을 구분).
  - 물 위에 차량이 있으면 차량은 빼지 말고 물 영역 경계만 크게 잡는다
    (인스턴스 분리가 목적이 아니라 침수 면적 산출이 목적).
  - 물이 전혀 없는 프레임도 **배경 샘플로 저장**해야 오탐이 줄어든다.
    (레터박스 오탐 사례 참고 — docs/yolo_license_alternatives.md 2-B절)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.common.video_io import list_folder_images

CLASS_ID = 0
CLASS_NAME = "flood_water"
POLY_COLOR = (229, 128, 0)      # BGR - 확정된 폴리곤(파랑 계열)
DRAW_COLOR = (0, 255, 255)      # BGR - 그리는 중(노랑)
OUT_ROOT = PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "labeled"


def _to_yolo_seg_line(poly: list[list[int]], w: int, h: int) -> str:
    coords = []
    for x, y in poly:
        coords.append(f"{min(max(x / w, 0.0), 1.0):.6f}")
        coords.append(f"{min(max(y / h, 0.0), 1.0):.6f}")
    return f"{CLASS_ID} " + " ".join(coords)


def _load_existing(label_path: Path, w: int, h: int) -> list[list[list[int]]]:
    if not label_path.exists():
        return []
    polys = []
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 7:  # class + 최소 3점(6좌표)
            continue
        vals = [float(v) for v in parts[1:]]
        pts = [[int(vals[i] * w), int(vals[i + 1] * h)] for i in range(0, len(vals) - 1, 2)]
        if len(pts) >= 3:
            polys.append(pts)
    return polys


class _WaterLabeler:
    def __init__(self, image_paths: list[Path], out_images: Path, out_labels: Path):
        self.paths = image_paths
        self.out_images = out_images
        self.out_labels = out_labels
        self.idx = 0
        self.polys: list[list[list[int]]] = []   # 확정된 폴리곤들
        self.cur: list[list[int]] = []           # 그리는 중인 폴리곤
        self.win = "Flood water labeler -- L:point R:undo ENTER:close n:next b:back d:clear q:quit"
        cv2.namedWindow(self.win, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self.win, self._on_mouse)

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.cur.append([x, y])
        elif event == cv2.EVENT_RBUTTONDOWN and self.cur:
            self.cur.pop()

    def _draw(self, frame):
        img = frame.copy()
        overlay = img.copy()
        for poly in self.polys:
            pts = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            cv2.fillPoly(overlay, [pts], POLY_COLOR)
            cv2.polylines(img, [pts], True, POLY_COLOR, 2)
        img = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)

        if self.cur:
            pts = np.asarray(self.cur, dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(img, [pts], False, DRAW_COLOR, 2)
            for p in self.cur:
                cv2.circle(img, tuple(p), 4, DRAW_COLOR, -1)

        area = sum(cv2.contourArea(np.asarray(p, dtype=np.int32)) for p in self.polys)
        ratio = area / (frame.shape[0] * frame.shape[1]) if self.polys else 0.0
        bar = (f"[{self.idx + 1}/{len(self.paths)}] {self.paths[self.idx].name}   "
               f"polygons={len(self.polys)}  drawing={len(self.cur)}pts  "
               f"water={ratio * 100:.1f}%")
        cv2.rectangle(img, (0, 0), (img.shape[1], 28), (0, 0, 0), -1)
        cv2.putText(img, bar, (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
        return img

    def _save(self, frame) -> None:
        h, w = frame.shape[:2]
        src = self.paths[self.idx]
        lines = [_to_yolo_seg_line(p, w, h) for p in self.polys]
        (self.out_labels / f"{src.stem}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        dst = self.out_images / src.name
        if not dst.exists():
            cv2.imwrite(str(dst), cv2.imread(str(src)))

    def run(self) -> None:
        n_labeled = 0
        while 0 <= self.idx < len(self.paths):
            src = self.paths[self.idx]
            frame = cv2.imread(str(src))
            if frame is None:
                print(f"[label_flood_water] 읽기 실패, 건너뜀: {src}")
                self.idx += 1
                continue
            h, w = frame.shape[:2]
            self.polys = _load_existing(self.out_labels / f"{src.stem}.txt", w, h)
            self.cur = []

            step = None
            while step is None:
                cv2.imshow(self.win, self._draw(frame))
                key = cv2.waitKey(30) & 0xFF
                if key in (13, 10):                      # ENTER: 폴리곤 확정
                    if len(self.cur) >= 3:
                        self.polys.append(list(self.cur))
                        self.cur = []
                    elif self.cur:
                        print(f"[label_flood_water] 폴리곤은 3점 이상 필요 (현재 {len(self.cur)}점)")
                elif key in (ord("n"), ord(" ")):
                    if len(self.cur) >= 3:               # 미확정 폴리곤 자동 확정
                        self.polys.append(list(self.cur))
                    self._save(frame)
                    n_labeled += 1
                    step = 1
                elif key == ord("b"):
                    step = -1
                elif key == ord("d"):
                    self.polys, self.cur = [], []
                elif key in (27, ord("q")):
                    cv2.destroyAllWindows()
                    print(f"[label_flood_water] 종료 - {n_labeled}장 저장됨 -> {self.out_labels}")
                    return
            self.idx += step
        cv2.destroyAllWindows()
        print(f"[label_flood_water] 완료 - {n_labeled}장 저장 -> {self.out_images} / {self.out_labels}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", required=True, help="라벨링할 이미지 폴더")
    ap.add_argument("--set-name", default=None,
                    help="출력 세트 이름 (기본: 입력 폴더 이름)")
    args = ap.parse_args()

    in_dir = Path(args.folder)
    if not in_dir.is_dir():
        raise SystemExit(f"폴더가 없습니다: {in_dir}")
    images = list_folder_images(in_dir)
    if not images:
        raise SystemExit(f"이미지가 없습니다: {in_dir}")

    set_name = args.set_name or in_dir.name
    out_images = OUT_ROOT / set_name / "images"
    out_labels = OUT_ROOT / set_name / "labels"
    out_images.mkdir(parents=True, exist_ok=True)
    out_labels.mkdir(parents=True, exist_ok=True)

    print(f"[label_flood_water] {len(images)}장 -- {in_dir}")
    print(f"[label_flood_water] 출력 -> {OUT_ROOT / set_name}")
    _WaterLabeler(images, out_images, out_labels).run()


if __name__ == "__main__":
    main()
