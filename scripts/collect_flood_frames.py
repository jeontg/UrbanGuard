"""강수 감지 시 CCTV 프레임을 자동 수집한다 (방안 B - 자체 학습 데이터 구축).

침수 학습 데이터의 최대 난점은 "실제로 비가 와야 데이터가 생긴다"는 점이다.
사람이 호우를 지켜보다 수동 실행할 수는 없으므로, 기상청(KMA) 실측 강수량을
주기적으로 조회해 임계값을 넘는 블록만 자동으로 프레임을 저장한다.

``--watch``로 상시 실행해두면 우기 동안 침수 데이터가 자동으로 쌓인다.

⚠️ **KMA_SERVICE_KEY가 반드시 필요합니다.** 키가 없으면 기존
``build_rainfall()``은 조용히 sine(가짜) 강수로 폴백해 **비가 오지 않는데도
임계값을 넘겨 쓸모없는 맑은 날 프레임만 쌓이게 됩니다.** 이를 막기 위해 이
스크립트는 ``KmaRainfallProvider``를 직접 생성하고 응답의 ``source``가 실제로
``kma``인지 검증하며, 아니면 해당 블록을 건너뜁니다.

출력: data/datasets/flood_water_own/raw/rain_auto/<block>_<timestamp>_<mm>mm.jpg
      (파일명에 강수량을 남겨 라벨링 우선순위 판단에 활용)

Usage:
    # 1회 조회
    python scripts/collect_flood_frames.py

    # 상시 감시 (10분 간격, 3mm/h 이상이면 수집)
    python scripts/collect_flood_frames.py --watch --interval 600 --threshold 3.0

    # 강수 무관하게 즉시 1장씩 (기준선/배경 샘플 확보용)
    python scripts/collect_flood_frames.py --force
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import cv2
from dotenv import load_dotenv

from tot_dashboard.common.blocks import grab_stream_frame, load_blocks, stream_url
from tot_dashboard.common.config import PROJECT_ROOT
from tot_dashboard.traffic_weather.perception.rainfall_provider import KmaRainfallProvider

OUT_DIR = PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "raw" / "rain_auto"


def _rain_mm_h(block: dict, key: str, cache: dict) -> float | None:
    """블록 좌표의 실측 강수량(mm/h). 실측이 아니면 None.

    같은 기상 격자에 속한 블록끼리 provider를 공유해 API 호출과 TTL 캐시를
    낭비하지 않는다(부산 도심 블록들은 대부분 같은 격자에 묶임).
    """
    c = block["coordinates"]
    try:
        provider = cache.get((round(c["lat"], 2), round(c["lng"], 2)))
        if provider is None:
            provider = KmaRainfallProvider(c["lat"], c["lng"], key)
            cache[(round(c["lat"], 2), round(c["lng"], 2))] = provider
        state = provider.at(0.0)
    except Exception as e:  # noqa: BLE001
        print(f"[collect_flood_frames]   {block['id']}: 강수 조회 실패 ({str(e)[:80]})")
        return None
    # sine/mock 폴백이 섞여 들어오면 가짜 강수로 수집이 트리거되므로 차단
    if getattr(state, "source", "") != "kma":
        print(f"[collect_flood_frames]   {block['id']}: 실측 아님(source={state.source}) - 건너뜀")
        return None
    return float(state.rain_mm_h)


def run_once(threshold: float, force: bool) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("KMA_SERVICE_KEY", "").strip()
    if not key and not force:
        print("[collect_flood_frames] KMA_SERVICE_KEY 미설정 - 강수 판정 불가.")
        print("[collect_flood_frames]   .env에 키를 넣거나, 강수 무관 수집은 --force 사용")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    cache: dict = {}
    saved = 0
    for b in load_blocks():
        url = stream_url(b)
        if not url:
            continue

        if force:
            mm = -1.0
        else:
            mm_or_none = _rain_mm_h(b, key, cache)
            if mm_or_none is None:
                continue
            mm = mm_or_none
            if mm < threshold:
                print(f"[collect_flood_frames]   {b['id']}: {mm:.1f}mm/h < {threshold} - 건너뜀")
                continue
            print(f"[collect_flood_frames]   {b['id']}: {mm:.1f}mm/h >= {threshold} - 수집!")

        frame = grab_stream_frame(url)
        if frame is None:
            print(f"[collect_flood_frames]   {b['id']}: 프레임 취득 실패")
            continue
        tag = "forced" if force else f"{mm:.0f}mm"
        out = OUT_DIR / f"{b['id']}_{ts}_{tag}.jpg"
        cv2.imwrite(str(out), frame)
        print(f"[collect_flood_frames]   저장: {out.name} ({frame.shape[1]}x{frame.shape[0]})")
        saved += 1
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=3.0,
                    help="이 강수량(mm/h) 이상일 때 수집 (기본 3.0)")
    ap.add_argument("--watch", action="store_true", help="상시 감시 모드(반복 실행)")
    ap.add_argument("--interval", type=int, default=600,
                    help="--watch 시 조회 간격(초, 기본 600). KMA 실황은 10분 주기 갱신")
    ap.add_argument("--force", action="store_true",
                    help="강수 판정 없이 전 블록 즉시 수집(배경 샘플 확보용)")
    args = ap.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    if not args.watch:
        n = run_once(args.threshold, args.force)
        print(f"\n[collect_flood_frames] 완료 - {n}장 저장 -> {OUT_DIR}")
        return

    print(f"[collect_flood_frames] 감시 시작 (임계 {args.threshold}mm/h, "
          f"{args.interval}초 간격). Ctrl+C로 종료")
    total = 0
    try:
        while True:
            print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
            total += run_once(args.threshold, args.force)
            print(f"[collect_flood_frames] 누적 {total}장")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[collect_flood_frames] 중단 - 누적 {total}장 -> {OUT_DIR}")


if __name__ == "__main__":
    main()
