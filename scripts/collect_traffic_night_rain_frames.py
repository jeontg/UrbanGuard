# -*- coding: utf-8 -*-
"""교통 야간·우천 학습 데이터 수집 — 2026-08-29, 「4대탐지기능 성능개선
로드맵」 4단계.

## 왜 필요한가

``scripts/train_traffic_vehicle_yolo.py``(2026-08-25 신설)는 이미 있지만
학습 데이터 자체가 0건이었다. 지금 운영 중인 차량검출은 COCO 사전학습
YOLO11s를 파인튜닝 없이 그대로 쓰는데, 야간·우천은 오탐·미탐이 가장
많이 나는 조건일 것으로 추정되면서도(측정 이력 없음 — 「4대탐지기능
성능개선 로드맵」 확인 필요 항목) 정작 그 조건의 학습 데이터가 없다.

## ⚠️ GPU가 필요하지 않다 — 앞선 로드맵 문서의 정정

이 항목은 원래 "GPU 확보 후" 항목으로 분류했으나, 침수 모델(LR-ASPP)이
**CPU만으로 학습**해 F1 0.9392를 낸 전례가 이미 이 프로젝트에 있다
(``docs/202608250759/detection_tech_stack.md``). ``train_traffic_vehicle_
yolo.py``도 ``--device cpu``가 기본값이다 — 시간이 더 걸릴 뿐 GPU는
필수가 아니다.

## 이 스크립트가 하는 일 — 딱 "수집"까지만

**실제 야간·실제 강수** 조건일 때만 CCTV 프레임을 저장한다. 언제가
"야간"·"우천"인지는 지어내지 않는다:

- 야간: 시각 기준(``crowd/field_sensors.py::MockEnvironmentProvider``와
  같은 규칙 — 실제 시계를 쓴다, 합성이 아니다)
- 우천: 기상청 초단기실황(RN1) 실측(``traffic_weather/perception/
  rainfall_provider.py::KmaRainfallProvider`` 재사용). ``KMA_SERVICE_KEY``
  환경변수가 없으면 우천 조건 확인 자체를 건너뛰고 **야간만으로 판단한다**
  (거짓으로 "비 온다"고 지어내지 않는다).

이 스크립트 하나를 **주기적으로**(예: 매시 실행하는 예약 작업) 돌려야
실제 야간·강수 순간의 프레임이 누적된다 — 1회 실행으로는 그 순간에
해당하지 않으면 아무것도 저장되지 않는 것이 정상이다.

## 다음 단계

수집된 원본은 ``scripts/label_traffic_vehicle_auto.py``로 가라벨(기존
사전학습 YOLO11s로 초안 생성)한 뒤, 사람이 확인하고
``scripts/train_traffic_vehicle_yolo.py``로 학습한다.

저장 위치: ``<보관소>/07_학습데이터_교통/traffic_vehicle_own/raw/
night_rain/<카메라ID>/<timestamp>_<조건>.jpg``
(``data/datasets/`` 아래 프로젝트 경로도 대체 가능 — ``common.data_
archive`` 해석 규칙과 동일)

Usage:
    python scripts/collect_traffic_night_rain_frames.py
    python scripts/collect_traffic_night_rain_frames.py --force-night   # 시험용
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

from tot_dashboard.common.data_archive import DATA_ARCHIVE_ROOT
from tot_dashboard.common.video_io import ascii_stem, imwrite_unicode

OUT_ROOT = (DATA_ARCHIVE_ROOT / "07_학습데이터_교통"
           / "traffic_vehicle_own" / "raw" / "night_rain")

NIGHT_START_HOUR = 20   # crowd/field_sensors.py::MockEnvironmentProvider와 동일 규칙
NIGHT_END_HOUR = 6
RAIN_MM_H_MIN = 0.1     # 이 이상이면 "비 온다"로 본다(약함 기준, calibration.py 참고 없음 — 자체 판단)


def _is_night(now: datetime) -> bool:
    h = now.hour
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR


def _check_rain(lat: float, lng: float) -> float | None:
    """실측 강수(mm/h). KMA 키가 없거나 조회 실패하면 None(모른다) — 0.0으로
    지어내지 않는다."""
    import os

    key = os.environ.get("KMA_SERVICE_KEY")
    if not key:
        return None
    try:
        from tot_dashboard.traffic_weather.perception.rainfall_provider import (
            KmaRainfallProvider,
        )
        provider = KmaRainfallProvider(lat=lat, lng=lng, service_key=key)
        state = provider.at(0.0)
        return float(state.rain_mm_h)
    except Exception as e:  # noqa: BLE001
        print(f"[collect_traffic_night_rain_frames] 강수 조회 실패({lat},{lng}): {str(e)[:100]}")
        return None


def _grab_hls_frame(url: str, timeout_tries: int = 30):
    import cv2

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", action="append", default=None,
                    help="카메라 id(여러 번 지정 가능, 생략 시 교통위험 사용 카메라 전체)")
    ap.add_argument("--force-night", action="store_true",
                    help="시험용 — 실제 시각과 무관하게 야간으로 취급(수집 로직 확인용)")
    args = ap.parse_args()

    from tot_dashboard.core import cameras as C
    from tot_dashboard.core.db import get_session

    db = get_session()
    try:
        cams = C.list_all(db, active_only=True)
    finally:
        db.close()

    if args.camera:
        wanted = set(args.camera)
        cams = [c for c in cams if c.id in wanted]

    now = datetime.now()
    is_night = args.force_night or _is_night(now)
    print(f"[collect_traffic_night_rain_frames] 실행 시각 {now:%Y-%m-%d %H:%M} "
         f"— 야간={is_night}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    saved, skipped_condition, skipped_source, failed = [], 0, 0, []

    for cam in cams:
        if cam.source_type != "hls" or not cam.source_url:
            skipped_source += 1
            continue

        rain_mm_h = None
        if cam.lat is not None and cam.lng is not None:
            rain_mm_h = _check_rain(cam.lat, cam.lng)
        is_rain = (rain_mm_h or 0.0) >= RAIN_MM_H_MIN

        if not (is_night or is_rain):
            skipped_condition += 1
            continue

        tags = "_".join(t for t in (
            "night" if is_night else "",
            "rain" if is_rain else "",
        ) if t)
        print(f"[collect_traffic_night_rain_frames] {cam.id} 수집 중 (조건: {tags})...")
        frame = _grab_hls_frame(cam.source_url)
        if frame is None:
            print(f"[collect_traffic_night_rain_frames]   실패: {cam.source_url}")
            failed.append(cam.id)
            continue

        out_dir = OUT_ROOT / ascii_stem(cam.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{ts}_{tags}.jpg"
        if imwrite_unicode(str(out_path), frame):
            print(f"[collect_traffic_night_rain_frames]   저장: {out_path} "
                 f"({frame.shape[1]}x{frame.shape[0]})")
            saved.append(str(out_path))
        else:
            failed.append(cam.id)

    print(f"\n[collect_traffic_night_rain_frames] 완료 — 저장 {len(saved)}건, "
         f"조건 안 맞아 건너뜀 {skipped_condition}건, HLS 아님 {skipped_source}건, "
         f"실패 {len(failed)}건")
    if failed:
        print(f"[collect_traffic_night_rain_frames] 실패한 카메라: {failed}")
    print(f"[collect_traffic_night_rain_frames] 저장 위치: {OUT_ROOT}")
    print("[collect_traffic_night_rain_frames] 이 스크립트를 주기적으로(예: 매시 예약 "
         "실행) 돌려야 실제 야간·강수 순간이 누적됩니다. 다음: "
         "scripts/label_traffic_vehicle_auto.py로 가라벨을 만드세요.")


if __name__ == "__main__":
    main()
