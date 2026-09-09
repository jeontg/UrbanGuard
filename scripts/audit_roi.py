"""등록된 모든 카메라의 ROI 를 진단한다 — 운영 반영 전 전수 점검용.

## 왜 필요한가 (2026-08-22 전수점검 후속)

2026-08-22부터 **저장된 ROI 가 실제 판정에 쓰인다**(그전에는 저장만 되고
아무도 읽지 않았다). 그래서 잘못 그린 ROI 는 이제 곧바로 오판이 된다 —
정체 감시 구역을 보도 위에 그리면 「상시 원활」, 도로 ROI 를 하늘에 그리면
「침수 없음」으로 보인다. **「안 보인다」와 「없다」가 같아 보이는** 종류라
사람이 눈치채기 가장 어렵다.

저장 검증은 「꼭짓점 3개 이상」만 보므로, 그 위에서 한 번 더 훑는 도구다.

## 사용법

    # 기하 검사만 (빠름, 모델·스트림 불필요)
    .venv\\Scripts\\python.exe scripts\\audit_roi.py

    # 교통 ROI 에 실제로 차량이 지나가는지까지 확인 (느림, 스트림·모델 필요)
    .venv\\Scripts\\python.exe scripts\\audit_roi.py --with-traffic

    # 특정 도메인만
    .venv\\Scripts\\python.exe scripts\\audit_roi.py --domain traffic

## 종료 코드

    0  확인이 필요한 항목 없음
    1  ERROR 가 하나 이상 (판정이 확실히 틀어지는 상태)
    2  WARN 만 있음 (사람이 봐야 하는 상태)

⚠️ **이 도구는 무엇도 고치지 않는다.** 전부 「확인해 보라」는 신호이며,
   정당한 설정인데 걸리는 경우가 있다(예: 왕복 2차로의 한쪽만 재려고 일부러
   좁게 그린 구역). 사람이 판단할 자리를 대신하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tot_dashboard.core import cameras as C  # noqa: E402
from tot_dashboard.core import roi_audit as RA  # noqa: E402
from tot_dashboard.core.db import get_session  # noqa: E402
from tot_dashboard.core.roles import DOMAIN_LABELS, Domain  # noqa: E402

_MARK = {RA.ERROR: "[오류]", RA.WARN: "[확인]", RA.INFO: "[참고]"}

# 교통량 검사에서 뜰 표본 프레임 수. 늘리면 정확해지지만 그만큼 오래 걸린다.
TRAFFIC_SAMPLE_FRAMES = 8


def _grab_vehicle_boxes(cam, frames: int) -> list[tuple] | None:
    """표본 프레임에서 차량 박스를 모은다. 실패하면 ``None``.

    ``model_probe`` 가 쓰는 것과 같은 검출기를 쓴다 — 진단이 실제 판정과
    다른 검출기를 보면 진단 결과를 믿을 수 없다.
    """
    try:
        import cv2
        from tot_dashboard.core import model_ops
        from tot_dashboard.traffic_weather.perception.detection_source import (
            VEHICLE_CLASSES,
        )
        from ultralytics import YOLO
    except Exception as e:  # noqa: BLE001
        print(f"    (교통량 검사 건너뜀 — 필요한 라이브러리 없음: {str(e)[:60]})")
        return None

    url = (cam.source_url or "").strip() or (cam.source_path or "").strip()
    if not url:
        return None
    key = model_ops.selected_key("traffic") or "models/yolo11s.pt"
    try:
        model = YOLO(key)
        cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            return None
        boxes: list[tuple] = []
        got = 0
        while got < frames:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            got += 1
            for r in model.predict(frame, verbose=False, conf=0.3):
                names = r.names
                for b in r.boxes:
                    if names.get(int(b.cls[0])) in VEHICLE_CLASSES:
                        boxes.append(tuple(float(v) for v in b.xyxy[0]))
        cap.release()
        return boxes
    except Exception as e:  # noqa: BLE001
        print(f"    (교통량 검사 실패: {str(e)[:80]})")
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", action="append", default=None,
                    choices=[d.value for d in Domain],
                    help="이 도메인만 본다(여러 번 지정 가능). 생략하면 전부.")
    ap.add_argument("--with-traffic", action="store_true",
                    help="교통 ROI 에 실제로 차량이 지나가는지 확인한다"
                         "(스트림·모델 필요, 지점당 수 초 소요).")
    ap.add_argument("--quiet-ok", action="store_true",
                    help="문제없는 지점은 출력하지 않는다.")
    args = ap.parse_args(argv)

    db = get_session()
    try:
        audits = RA.audit_all(db, domains=args.domain)
        cams = {c.id: c for c in C.list_all(db)}
    finally:
        db.close()

    if not audits:
        print("[audit-roi] 진단할 대상이 없습니다 "
              "(탐지 지정이 켜진 카메라·도메인이 없습니다).")
        return 0

    n_err = n_warn = n_ok = 0
    for a in audits:
        cam = cams.get(a.camera_id)
        label = DOMAIN_LABELS.get(Domain(a.domain), a.domain)
        name = cam.name if cam else a.camera_id

        findings = list(a.findings)
        # 교통량 검사는 기하 검사에서 이미 ERROR 가 난 지점에는 하지 않는다 —
        # 어차피 다시 그려야 하므로 스트림을 뜨는 비용이 아깝다.
        if (args.with_traffic and a.domain == Domain.TRAFFIC.value
                and cam is not None
                and not any(f.severity == RA.ERROR for f in findings)):
            roi = C.roi_of(db := get_session(), a.camera_id, a.domain)
            db.close()
            polys = (roi.get("shapes") or {}).get("congestion_roi") or []
            if polys:
                boxes = _grab_vehicle_boxes(cam, TRAFFIC_SAMPLE_FRAMES)
                if boxes is not None:
                    f = RA.audit_traffic_overlap(polys, boxes)
                    if f is not None:
                        findings.append(f)

        worst = ""
        for lvl in (RA.ERROR, RA.WARN, RA.INFO):
            if any(f.severity == lvl for f in findings):
                worst = lvl
                break
        if worst == RA.ERROR:
            n_err += 1
        elif worst == RA.WARN:
            n_warn += 1
        else:
            n_ok += 1
            if args.quiet_ok and not findings:
                continue

        head = f"{name} ({a.camera_id}) · {label}"
        if not findings:
            print(f"  [정상] {head}")
            continue
        print(f"  {_MARK.get(worst, '')} {head}")
        for f in findings:
            print(f"        {_MARK.get(f.severity, '')} {f.shape}: {f.message}")

    print()
    print(f"[audit-roi] 대상 {len(audits)}건 — "
          f"오류 {n_err} · 확인필요 {n_warn} · 정상 {n_ok}")
    if n_err:
        print("  ⚠ [오류] 는 판정이 확실히 틀어지는 상태입니다 — 다시 그려야 합니다.")
    if n_warn:
        print("  ⚠ [확인] 은 틀렸을 가능성이 높다는 신호일 뿐입니다 — "
              "정당한 설정일 수도 있으니 사람이 판단하십시오.")
    return 1 if n_err else (2 if n_warn else 0)


if __name__ == "__main__":
    raise SystemExit(main())
