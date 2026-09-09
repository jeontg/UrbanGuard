"""CCTV 스트림의 **실제 해상도**를 재서 고해상도 지점을 골라낸다.

왜 실측하는가
    ITS OpenAPI 의 ``cctvresolution`` 은 비어 있거나 실제와 다를 수 있다.
    관제·탐지 품질을 좌우하는 것은 표기가 아니라 우리가 실제로 받는
    프레임의 크기다. 그래서 **한 프레임을 실제로 받아 보고** 잰다.

무엇을 재는가
    가로×세로, 초당 프레임, 첫 프레임까지 걸린 시간. 접속 실패도 결과로
    남긴다 — 「목록에는 있는데 안 열리는 지점」이 실제로 많고, 그것을
    모른 채 등록하면 상시 탐지가 조용히 비어 버린다.

사용
    # ① 교통정보 API 로 지역 목록을 받아 그대로 잰다 (ITS_API_KEY 필요)
    python scripts/probe_cctv_resolution.py --region seoul --limit 30

    # ② 이미 등록된 지점을 잰다
    python scripts/probe_cctv_resolution.py --registered

    # ③ 주소를 직접 준다
    python scripts/probe_cctv_resolution.py --url https://.../stream.m3u8

    # 결과를 CSV 로
    python scripts/probe_cctv_resolution.py --region jeju --csv out.csv
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 윈도우 콘솔 기본값이 cp949 라 「—」 같은 글자에서 출력이 통째로 죽는다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # 파이프로 넘길 때는 그냥 둔다
        pass

import cv2  # noqa: E402

from tot_dashboard.core import cctv_sources as SRC  # noqa: E402

# 첫 프레임을 못 받으면 접는다. 관제망에서 막힌 주소를 오래 붙잡고 있으면
# 전체 조사가 끝나지 않는다.
OPEN_TIMEOUT_MS = 8000


def probe(name: str, url: str) -> dict:
    """한 지점을 열어 첫 프레임 크기를 잰다. 실패도 결과로 돌려준다."""
    started = time.monotonic()
    row = {"name": name, "url": url, "width": 0, "height": 0,
           "fps": 0.0, "sec": 0.0, "note": ""}
    cap = None
    try:
        # FFMPEG 백엔드에 열기·읽기 제한시간을 준다(마이크로초 단위 옵션).
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            f"timeout;{OPEN_TIMEOUT_MS * 1000}|stimeout;{OPEN_TIMEOUT_MS * 1000}")
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            row["note"] = "열리지 않음"
            return row
        ok, frame = cap.read()
        if not ok or frame is None:
            row["note"] = "열렸으나 프레임 없음"
            return row
        # 속성값(CAP_PROP_*)이 아니라 **받은 배열의 모양**을 쓴다. 속성은
        # 컨테이너가 신고한 값이라 실제 디코딩 결과와 어긋나는 일이 있다.
        h, w = frame.shape[:2]
        row["width"], row["height"] = int(w), int(h)
        # fps 는 컨테이너가 신고한 값이라 종종 엉뚱하다. 실제로 90000(=90kHz
        # time_base)이 그대로 올라오는 스트림을 봤다. 말이 되는 범위 밖은
        # **0 으로 두고 「미상」으로 표시한다** — 90000fps 를 그대로 실으면
        # 그 표를 보는 사람이 다른 값까지 못 믿게 된다.
        fps = cap.get(cv2.CAP_PROP_FPS)
        row["fps"] = round(float(fps), 1) if fps and 0 < fps <= 120 else 0.0
        return row
    except Exception as e:  # noqa: BLE001
        row["note"] = f"오류: {str(e)[:60]}"
        return row
    finally:
        if cap is not None:
            cap.release()
        row["sec"] = round(time.monotonic() - started, 1)


def targets_from_region(region: str, limit: int) -> list[tuple[str, str]]:
    rows = SRC.fetch(region)
    return [(r["name"], r["source_url"]) for r in rows[:limit]]


def targets_registered() -> list[tuple[str, str]]:
    from tot_dashboard.core.db import session_factory
    from tot_dashboard.core.models import Camera
    with session_factory()() as s:
        out = []
        for c in s.query(Camera).order_by(Camera.id).all():
            # 파일 기반 지점(source_path)은 스트림이 아니라 건너뛴다.
            if c.source_url:
                out.append((f"{c.id} {c.name}", c.source_url))
        return out


def grade(h: int) -> str:
    if h >= 2160:
        return "UHD"
    if h >= 1080:
        return "FHD"
    if h >= 720:
        return "HD"
    if h > 0:
        return "SD"
    return "—"


def main() -> int:
    ap = argparse.ArgumentParser(description="CCTV 실제 해상도 측정")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--region", choices=sorted(SRC.REGIONS), help="지역 프리셋")
    src.add_argument("--registered", action="store_true", help="등록된 지점")
    src.add_argument("--url", nargs="+", help="주소 직접 지정")
    ap.add_argument("--limit", type=int, default=40, help="최대 지점 수")
    ap.add_argument("--workers", type=int, default=6, help="동시 측정 수")
    ap.add_argument("--csv", help="결과를 CSV 로 저장")
    args = ap.parse_args()

    if args.region:
        try:
            items = targets_from_region(args.region, args.limit)
        except SRC.SourceError as e:
            print(f"목록을 받지 못했습니다 — {e}", file=sys.stderr)
            return 2
    elif args.registered:
        items = targets_registered()[:args.limit]
    else:
        items = [(u, u) for u in args.url]

    if not items:
        print("측정할 지점이 없습니다.", file=sys.stderr)
        return 1

    print(f"{len(items)}개 지점을 측정합니다 (동시 {args.workers})…\n")
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda it: probe(*it), items))

    # 해상도 높은 순. 실패는 맨 뒤로.
    rows.sort(key=lambda r: (-(r["height"]), r["name"]))

    ok = [r for r in rows if r["height"] > 0]
    print(f"{'등급':<5}{'해상도':>12}{'fps':>7}{'초':>6}  지점")
    print("-" * 78)
    for r in rows:
        res = f"{r['width']}x{r['height']}" if r["height"] else (r["note"] or "실패")
        fps = f"{r['fps']:g}" if r["fps"] else "미상"
        print(f"{grade(r['height']):<5}{res:>12}{fps:>7}{r['sec']:>6}  {r['name'][:38]}")

    print("-" * 78)
    print(f"성공 {len(ok)} / 전체 {len(rows)}")
    if ok:
        for g in ("UHD", "FHD", "HD", "SD"):
            n = sum(1 for r in ok if grade(r["height"]) == g)
            if n:
                print(f"  {g:<4} {n}개소")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n저장: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
