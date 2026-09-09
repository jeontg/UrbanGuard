"""SVRDD → 프로젝트 2클래스(pothole/crack) YOLO 데이터셋 변환.

왜 SVRDD 인가
    RDD2022(체코, 차량 블랙박스 근접)와 Mendeley(인도네시아, 스마트폰 130cm)는
    둘 다 부산 CCTV에서 **탐지 0건**이었다. 두 번의 실패가 모두 도메인 갭이었고,
    공통점은 **구도**였다 — 노면을 정면에서 가깝게 본 사진들이다.

    SVRDD 는 차량 지붕 스트리트뷰 장비로 찍은 **pitch 0°와 45°** 이미지를
    합쳐 만든 데이터셋이다. 45° 부감은 고가에 달린 관제 CCTV가 내려다보는
    각도에 지금까지 확보한 어떤 데이터보다 가깝다. 그래서 「이 각도면 되는가」를
    적은 비용으로 판별할 수 있는 첫 후보다.

라이선스
    CC BY 4.0 (Zenodo 10100129). 출처를 표시하면 상업적 이용도 가능하다.
    ⚠️ 다만 원본이 바이두 지도 스트리트뷰라 **바이두 오픈플랫폼 약관을 함께
    지켜야 한다**고 배포처가 명시하고 있다. 납품 전 법무 확인이 필요하다.

클래스 매핑
    SVRDD 는 7종을 구분하지만 이 프로젝트의 등급 체계는 포트홀·균열 둘만
    쓴다(``road/defect_detection.py`` DEFAULT_CLASS_NAMES).

    ====================  ==========  ====================================
    SVRDD                 → 우리      이유
    ====================  ==========  ====================================
    pothole               0 pothole   그대로
    longitudinal crack    1 crack     균열 3종을 하나로 합친다
    transverse crack      1 crack
    alligator crack       1 crack
    longitudinal patch    제외        **보수 흔적**이지 손상이 아니다
    transverse patch      제외
    manhole cover         제외        시설물이다
    ====================  ==========  ====================================

    패치·맨홀이 든 이미지를 버리지는 않는다. 라벨만 지우고 **배경(negative)**
    으로 남긴다 — 포트홀과 생김새가 닮아서, 「이건 손상이 아니다」를 가르치는
    데 오히려 값진 표본이다.

Usage:
    python scripts/prepare_svrdd_yolo.py                    # 기본 경로
    python scripts/prepare_svrdd_yolo.py --src <풀어놓은 경로>
    python scripts/prepare_svrdd_yolo.py --val-ratio 0.1
"""
from __future__ import annotations

import argparse
import shutil
from collections import Counter
from pathlib import Path

from tot_dashboard.common.config import PROJECT_ROOT

SRC_DEFAULT = PROJECT_ROOT / "data" / "datasets" / "svrdd" / "extracted"
OUT_DEFAULT = PROJECT_ROOT / "data" / "datasets" / "svrdd" / "yolo"

IMG_SUFFIX = {".jpg", ".jpeg", ".png"}

# SVRDD 의 클래스 이름 → 우리 클래스 번호. None 이면 라벨을 버린다.
# 배포본의 클래스 **순서**를 신뢰하지 않고 이름으로 맞춘다 — 순서가 바뀌면
# 조용히 포트홀이 균열로 학습되는데, 그런 오류는 나중에 찾기가 매우 어렵다.
NAME_MAP: dict[str, int | None] = {
    "pothole": 0,
    "longitudinal crack": 1,
    "transverse crack": 1,
    "alligator crack": 1,
    "longitudinal patch": None,
    "transverse patch": None,
    "manhole cover": None,
    # 배포본에 따라 이름이 축약돼 있을 수 있어 별칭도 받는다.
    "longitudinal_crack": 1, "transverse_crack": 1, "alligator_crack": 1,
    "longitudinal_patch": None, "transverse_patch": None,
    "manhole": None, "manhole_cover": None,
    "d00": 1, "d10": 1, "d20": 1, "d40": 0,     # RDD 표기를 쓰는 경우
}

OUR_NAMES = ["pothole", "crack"]

# ⚠️ 배포본(SVRDD_YOLO.zip)에는 **클래스 이름 파일이 들어 있지 않다.**
# train/val/test 분할 목록만 있고 data.yaml·classes.txt 가 없다.
#
# 그래서 2026-08-12 에 라벨 박스를 **잘라 눈으로 확인해** 순서를 확정했다.
# 논문 초록의 나열 순서("… pothole, longitudinal patch, transverse patch,
# and manhole cover")와 **다르다** — 초록대로 믿었으면 맨홀을 균열로 학습할
# 뻔했다. 서술 순서는 라벨 순서가 아니다.
#
#   ==  ====================  ======  =========================================
#   ID  확인된 내용            건수    근거
#   ==  ====================  ======  =========================================
#   0   종방향 균열            4,665   주행 방향으로 난 가는 균열
#   1   횡방향 균열            3,404   차선을 가로지르는 가는 균열
#   2   거북등 균열            1,728   그물처럼 얽힌 균열망
#   3   **포트홀**               918   패인 구멍 — 가장 뚜렷하고 가장 적다
#   4   **맨홀 뚜껑**          3,339   원형 금속 뚜껑 — 손상이 아니다
#   5   종방향 보수흔           4,128   실링재(밝은 융기선/타르선) — 이미 보수됨
#   6   횡방향 보수흔           2,622   같은 실링재가 차선을 가로지름
#   ==  ====================  ======  =========================================
#
# 다시 확인하려면 라벨 박스를 잘라 보면 된다(위 표를 만든 방법과 같다).
SVRDD_ORDER = [
    "longitudinal crack", "transverse crack", "alligator crack",
    "pothole", "manhole cover", "longitudinal patch", "transverse patch",
]


def find_classes_file(src: Path) -> Path | None:
    """클래스 이름 목록 파일(data.yaml / classes.txt / *.names)을 찾는다."""
    for pattern in ("**/data.yaml", "**/*.yaml", "**/classes.txt", "**/*.names"):
        for p in sorted(src.glob(pattern)):
            if p.is_file():
                return p
    return None


def parse_class_names(path: Path) -> list[str]:
    """클래스 이름을 순서대로 읽는다. yaml 파서 없이 최소한으로 훑는다."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".yaml", ".yml"):
        names: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("names:"):
                rest = line.split(":", 1)[1].strip()
                if rest.startswith("["):
                    inner = rest.strip("[]")
                    return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
                names = []          # 아래 목록 형태
            elif names is not None and line.startswith("- "):
                names.append(line[2:].strip().strip("'\""))
        return names
    return [x.strip() for x in text.splitlines() if x.strip()]


def build_index_map(names: list[str]) -> dict[int, int | None]:
    """SVRDD 클래스 번호 → 우리 번호. 모르는 이름은 오류로 세운다."""
    out: dict[int, int | None] = {}
    unknown = []
    for i, raw in enumerate(names):
        key = raw.strip().lower()
        if key in NAME_MAP:
            out[i] = NAME_MAP[key]
        else:
            unknown.append(raw)
            out[i] = None
    if unknown:
        print(f"[경고] 매핑을 모르는 클래스 {unknown} — 해당 라벨은 버립니다.")
    return out


def official_splits(src: Path) -> dict[str, str]:
    """배포본의 train/val/test 목록을 읽어 {파일이름: split} 로 만든다.

    자체 분할(N개 중 1개)보다 **배포본 분할을 쓰는 편이 낫다** — 논문에 실린
    성능과 같은 기준으로 비교할 수 있고, 같은 장소를 찍은 인접 프레임이
    train 과 val 에 갈라져 들어가 성능이 부풀려지는 일도 막아 준다.

    ⚠️ test 는 학습에 쓰지 않는다. 최종 평가용으로 남긴다.
    """
    out: dict[str, str] = {}
    for split in ("train", "val", "test"):
        f = next((p for p in src.rglob(f"{split}.txt")
                  if "labels" not in p.parts), None)
        if f is None:
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().replace("\\", "/")
            if line:
                out[Path(line).name] = split
    return out


def label_for(img: Path) -> Path | None:
    """이미지에 대응하는 라벨 파일. images/ ↔ labels/ 규칙을 우선 본다."""
    parts = list(img.parts)
    if "images" in parts:
        parts[len(parts) - 1 - parts[::-1].index("images")] = "labels"
        cand = Path(*parts).with_suffix(".txt")
        if cand.is_file():
            return cand
    cand = img.with_suffix(".txt")
    return cand if cand.is_file() else None


def convert_label(path: Path, idx_map: dict[int, int | None]) -> tuple[list[str], Counter]:
    """라벨 한 파일을 우리 클래스로 옮긴다."""
    kept: list[str] = []
    stat: Counter = Counter()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            src_cls = int(float(parts[0]))
        except ValueError:
            continue
        dst = idx_map.get(src_cls)
        if dst is None:
            stat["dropped"] += 1
            continue
        kept.append(" ".join([str(dst), *parts[1:5]]))
        stat[OUR_NAMES[dst]] += 1
    return kept, stat


def main() -> None:
    ap = argparse.ArgumentParser(description="SVRDD → 2클래스 YOLO 변환")
    ap.add_argument("--src", type=Path, default=SRC_DEFAULT,
                    help="SVRDD 압축을 푼 폴더")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--val-ratio", type=int, default=10,
                    help="N개 중 1개를 검증셋으로 뗀다(기본 10)")
    args = ap.parse_args()

    src: Path = args.src
    if not src.is_dir():
        print(f"[오류] 원본 폴더가 없습니다: {src}")
        print("      SVRDD_YOLO.zip 을 먼저 풀어 주세요.")
        return

    cls_file = find_classes_file(src)
    names = parse_class_names(cls_file) if cls_file else []
    if names:
        print(f"[svrdd] 클래스 정의: {cls_file}")
    else:
        # 배포본에 클래스 파일이 없다. 추측이 아니라 **눈으로 확인한** 순서를
        # 쓴다(위 SVRDD_ORDER 주석 참고). 그래도 사용자가 알고 있어야 한다.
        names = list(SVRDD_ORDER)
        print("[svrdd] ⚠ 배포본에 클래스 이름 파일이 없어 **확인된 기본 순서**를")
        print("        씁니다 (2026-08-12 라벨 박스 육안 확인).")
        print("        논문 초록의 나열 순서와 다릅니다 — 맨홀이 4번입니다.")
        print("        다른 배포본을 쓴다면 --src 안에 data.yaml 을 넣어 주세요.")
    for i, n in enumerate(names):
        dst = NAME_MAP.get(n.strip().lower())
        label = OUR_NAMES[dst] if dst is not None else "제외"
        print(f"         {i:2d} {n:22s} → {label}")
    idx_map = build_index_map(names)

    images = sorted(p for p in src.rglob("*") if p.suffix.lower() in IMG_SUFFIX)
    if not images:
        print(f"[오류] 이미지를 찾지 못했습니다: {src}")
        return

    out: Path = args.out
    for split in ("train", "val", "test"):
        for kind in ("images", "labels"):
            (out / split / kind).mkdir(parents=True, exist_ok=True)

    splits = official_splits(src)
    if splits:
        print(f"[svrdd] 배포본 분할 사용 — {Counter(splits.values())}")
    else:
        print(f"[svrdd] 배포본 분할이 없어 {args.val_ratio}개 중 1개를 val 로 뗍니다.")

    total = Counter()
    n_img = Counter()
    n_bg = 0
    # 난수를 쓰지 않는다 — 다시 돌려도 같은 분할이 나와야 결과를 비교할 수 있다.
    for i, img in enumerate(images):
        split = splits.get(img.name) or ("val" if (i % args.val_ratio == 0)
                                         else "train")
        lbl = label_for(img)
        lines, stat = convert_label(lbl, idx_map) if lbl else ([], Counter())
        total.update(stat)
        if not lines:
            n_bg += 1
        # 파일명이 겹치지 않도록 상위 폴더 이름을 붙인다.
        stem = f"{img.parent.name}_{img.stem}" if img.parent.name else img.stem
        shutil.copy2(img, out / split / "images" / f"{stem}{img.suffix.lower()}")
        (out / split / "labels" / f"{stem}.txt").write_text(
            "\n".join(lines), encoding="utf-8")
        n_img[split] += 1

    (out / "data.yaml").write_text(
        f"train: {(out / 'train' / 'images').as_posix()}\n"
        f"val: {(out / 'val' / 'images').as_posix()}\n"
        "nc: 2\n"
        "names: ['pothole', 'crack']\n",
        encoding="utf-8")

    print(f"\n[svrdd] 이미지 train {n_img['train']} / val {n_img['val']}"
          f" / test {n_img['test']} (test 는 학습에 쓰지 않습니다)")
    print(f"[svrdd] 라벨 pothole {total['pothole']} · crack {total['crack']}"
          f" · 제외 {total['dropped']}")
    print(f"[svrdd] 라벨 없는 배경 이미지 {n_bg}장 (오탐 억제용으로 함께 학습)")
    print(f"[svrdd] data.yaml: {out / 'data.yaml'}")
    print("\n다음 단계:  python scripts/train_svrdd.py")


if __name__ == "__main__":
    main()
