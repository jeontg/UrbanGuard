"""Phase 3 — 어느 CCTV 지점이 노면 라벨링에 쓸 만한지 판정한다.

왜 필요한가
    RDD2022 모델이 부산 CCTV에서 0건인 원인은 도메인 갭이고, 해결책은 자체
    데이터 라벨링뿐이다(``docs/road_surface_management_plan.md`` Phase 2).
    그런데 **모든 지점을 라벨링할 수는 없다.** 실측해 보니 등록된 지점 대부분이
    320~352×240이고, 그 해상도에서는 고가에 달린 광각 카메라가 잡은 노면이
    몇 픽셀에 불과해 **사람도 포트홀 박스를 그을 수 없다.** 라벨을 그을 수
    없으면 학습은 시작조차 되지 않는다.

    그래서 라벨링 인력을 투입하기 전에 「이 지점은 가능한가」를 먼저 가른다.
    28장을 모으는 데 든 노력이 헛되지 않으려면 이 판정이 먼저다.

무엇을 재는가
    해상도(1차 기준)
        짧은 변이 240px이면 노면이 화면 아래쪽 일부에만 잡히고, 손상은 몇
        픽셀이 된다. 판정의 대부분은 여기서 갈린다.
    선명도(라플라시안 분산) — **하한 검사용이지 순위 지표가 아니다**
        초점이 나갔거나 신호가 깨진 화면을 걸러내는 용도다. 실측해 보니
        320×240 지점이 720×480 지점보다 값이 **높게** 나왔다(1558 vs 6267) —
        저해상도일수록 압축 잡음이 에지로 잡히기 때문이다. 그러니 이 값이
        크다고 라벨링이 쉬운 것이 아니다. 값이 바닥일 때만 의미가 있다.
    노면 밝기 편차
        화면 아래쪽(카메라에 가까운 노면) 영역의 밝기 표준편차다. 값이 너무
        작으면 균질한 아스팔트만 보이는 것이라 손상이 있어도 드러나지 않는다.

⚠️ 이 판정은 **선별용 참고치**이지 최종 결론이 아니다. 마지막 판단은 사람이
표본 이미지를 눈으로 보고 해야 하며, 그래서 지점마다 표본을 함께 저장한다.

Usage:
    python scripts/assess_road_label_feasibility.py
    python scripts/assess_road_label_feasibility.py --from-samples   # 이미 모은 프레임으로만
    python scripts/assess_road_label_feasibility.py --seconds 8
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from tot_dashboard.common.config import PROJECT_ROOT

OUT_DIR = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "feasibility"
RAW_DIR = PROJECT_ROOT / "data" / "datasets" / "road_cctv_own" / "raw"

# 판정 기준 — 짧은 변 기준이다. 긴 변으로 재면 21:9 광각이 실제보다 좋게 나온다.
MIN_SHORT_OK = 540        # 720×540 이상이면 근거리 차선 라벨링 여지가 있다
MIN_SHORT_MAYBE = 360     # 이 아래(=240p)는 사람도 박스를 그을 수 없었다
# 선명도 하한. 라플라시안 분산은 절대 기준이 없어 표본으로 잡은 값이다.
SHARP_OK = 80.0
SHARP_LOW = 30.0

VERDICT_OK = "가능성 있음"
VERDICT_MAYBE = "조건부"
VERDICT_NO = "불가"


def road_cameras() -> list[dict]:
    """노면 대상 카메라. DB(S-80)가 정본이고, 없으면 blocks.json 으로 내려간다."""
    try:
        from tot_dashboard.core import cameras as C
        from tot_dashboard.core import roles as R
        from tot_dashboard.core.db import get_session
        db = get_session()
        try:
            out = []
            for c in C.for_domain(db, R.Domain.ROAD.value):
                url = (c.source_url if c.source_type == "hls"
                       else c.source_path if c.source_type == "video" else "")
                out.append({"id": c.id, "name": c.name, "url": url or "",
                            "source_type": c.source_type})
            if out:
                return out
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[경고] DB에서 카메라를 읽지 못했습니다({str(e)[:80]}). "
              f"blocks.json 으로 대체합니다.")
    from tot_dashboard.common.blocks import load_blocks, stream_url
    return [{"id": b["id"], "name": b["name"], "url": stream_url(b) or "",
             "source_type": "hls"} for b in load_blocks()]


def grab_frame(url: str, seconds: float) -> "np.ndarray | None":
    """스트림에서 프레임 한 장. 버퍼된 첫 장 대신 잠시 읽다가 최신 것을 쓴다."""
    if not url:
        return None
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return None
    last, deadline = None, time.time() + max(seconds, 1.0)
    while time.time() < deadline:
        ok, fr = cap.read()
        if ok and fr is not None:
            last = fr
        else:
            time.sleep(0.05)
    cap.release()
    return last


def imread(path: Path):
    """한글 경로에서도 읽는다.

    ``cv2.imread`` 는 Windows 에서 한글이 섞인 경로를 읽지 못하고 조용히
    ``None`` 을 돌려준다. 설치 경로에 기관명이 한글로 들어가면 「프레임을 받지
    못했습니다」로만 보여 원인을 찾기 어렵다.
    """
    try:
        return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None


def imwrite(path: Path, frame) -> bool:
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        return False
    try:
        path.write_bytes(buf.tobytes())
        return True
    except OSError:
        return False


def sample_from_disk(camera_id: str) -> "np.ndarray | None":
    """이미 모아 둔 프레임에서 가장 최근 것."""
    d = RAW_DIR / camera_id
    files = sorted(p for p in d.glob("*.jpg")) if d.is_dir() else []
    return imread(files[-1]) if files else None


def measure(frame) -> dict:
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    # 화면 아래 1/3 = 카메라에 가장 가까운 노면. 여기가 라벨링이 가능한
    # 유일한 구간이므로, 전체 평균이 아니라 이 영역만 본다.
    near = gray[int(h * 2 / 3):, :]
    return {"width": w, "height": h, "short_side": min(w, h),
            "sharpness": round(sharp, 1),
            "near_contrast": round(float(near.std()), 1)}


def judge(m: dict) -> tuple[str, str]:
    short, sharp = m["short_side"], m["sharpness"]
    if short < MIN_SHORT_MAYBE:
        return VERDICT_NO, (f"짧은 변 {short}px — 노면 손상이 몇 픽셀에 불과해 "
                            f"사람도 박스를 그을 수 없습니다.")
    if sharp < SHARP_LOW:
        return VERDICT_NO, (f"선명도 {sharp} — 표면 질감이 남아 있지 않습니다"
                            f"(압축·초점). 해상도와 무관하게 어렵습니다.")
    if short < MIN_SHORT_OK:
        return VERDICT_MAYBE, (f"짧은 변 {short}px — 큰 포트홀만, 근거리 차선에 "
                               f"한해 시도해 볼 수 있습니다.")
    if sharp < SHARP_OK:
        return VERDICT_MAYBE, (f"해상도는 충분하나 선명도 {sharp} 로 낮습니다. "
                               f"표본을 눈으로 확인하세요.")
    if m["near_contrast"] < 12:
        return VERDICT_MAYBE, (f"근거리 노면 밝기 편차 {m['near_contrast']} — "
                               f"균질해 손상이 드러나지 않을 수 있습니다.")
    return VERDICT_OK, (f"짧은 변 {short}px · 선명도 {sharp} — 근거리 차선 "
                        f"라벨링을 시도할 만합니다.")


def main() -> None:
    ap = argparse.ArgumentParser(description="노면 라벨링 적합도 평가")
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="지점당 스트림 관측 시간(초)")
    ap.add_argument("--from-samples", action="store_true",
                    help="스트림에 붙지 않고 이미 모은 프레임만 쓴다")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cams = road_cameras()
    if not cams:
        print("[오류] 노면 대상 카메라가 없습니다. CCTV 관리(S-80)에서 지정하세요.")
        return

    print(f"노면 라벨링 적합도 평가 — {len(cams)}개 지점\n")
    rows = []
    for cam in cams:
        frame = None
        origin = ""
        if not args.from_samples:
            frame = grab_frame(cam["url"], args.seconds)
            origin = "실시간" if frame is not None else ""
        if frame is None:
            frame = sample_from_disk(cam["id"])
            origin = origin or ("보관 표본" if frame is not None else "")
        if frame is None:
            print(f"  ✗ {cam['name']:20s} 프레임을 받지 못했습니다")
            rows.append({**cam, "verdict": "확인 불가", "origin": "",
                         "why": "스트림에 붙지 못했고 보관된 표본도 없습니다.",
                         "width": 0, "height": 0, "short_side": 0,
                         "sharpness": 0, "near_contrast": 0})
            continue

        m = measure(frame)
        verdict, why = judge(m)
        # 최종 판단은 사람이 눈으로 한다. 그러라고 표본을 남긴다.
        shot = OUT_DIR / f"{cam['id']}.jpg"
        imwrite(shot, frame)
        mark = {VERDICT_OK: "○", VERDICT_MAYBE: "△", VERDICT_NO: "✗"}.get(verdict, "?")
        print(f"  {mark} {cam['name']:20s} {m['width']}×{m['height']}  "
              f"선명도 {m['sharpness']:7.1f}  {verdict}")
        rows.append({**cam, **m, "verdict": verdict, "why": why,
                     "origin": origin, "sample": str(shot)})

    _write_report(rows, args)
    ok = [r for r in rows if r["verdict"] == VERDICT_OK]
    maybe = [r for r in rows if r["verdict"] == VERDICT_MAYBE]
    print(f"\n가능성 있음 {len(ok)} · 조건부 {len(maybe)} · "
          f"불가 {len(rows) - len(ok) - len(maybe)}")
    if not ok and not maybe:
        print("\n⚠ 라벨링을 시도할 지점이 없습니다. 현재 등록된 CCTV로는 자체")
        print("  데이터 구축이 성립하지 않으며, 노면 전용 촬영(차량 탑재·저각")
        print("  설치) 도입 여부를 결정해야 합니다.")
    else:
        first = (ok or maybe)[0]
        print(f"\n다음 단계 — 표본을 눈으로 확인한 뒤 라벨링을 시작하세요:")
        print(f"  python scripts/label_road_defects.py --block {first['id']}")


def _write_report(rows: list[dict], args) -> None:
    """판단 근거를 남긴다 — 결정은 전략기획부가 한다."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 노면 라벨링 적합도 평가",
        "",
        f"- 평가 일시: {stamp}",
        f"- 대상: {len(rows)}개 지점",
        f"- 관측: {'보관 표본만' if args.from_samples else f'지점당 {args.seconds}초'}",
        "",
        "> ⚠️ 이 판정은 **선별용 참고치**입니다. 최종 판단은 표본 이미지를",
        "> 눈으로 확인해 주십시오. 표본 저장 위치: "
        f"`{OUT_DIR}`",
        "",
        "## 판정 결과",
        "",
        "| 지점 | 해상도 | 선명도 | 근거리 편차 | 판정 | 사유 |",
        "|---|---|---|---|---|---|",
    ]
    order = {VERDICT_OK: 0, VERDICT_MAYBE: 1, VERDICT_NO: 2}
    for r in sorted(rows, key=lambda x: order.get(x["verdict"], 3)):
        res = f"{r['width']}×{r['height']}" if r["width"] else "—"
        lines.append(f"| {r['name']} | {res} | {r['sharpness']} | "
                     f"{r['near_contrast']} | **{r['verdict']}** | {r['why']} |")
    lines += [
        "",
        "## 판정 기준",
        "",
        f"- **불가** — 짧은 변 {MIN_SHORT_MAYBE}px 미만, 또는 선명도 {SHARP_LOW} 미만",
        f"- **조건부** — 짧은 변 {MIN_SHORT_OK}px 미만, 또는 선명도 {SHARP_OK} 미만",
        f"- **가능성 있음** — 위 조건을 모두 넘김",
        "",
        "짧은 변으로 재는 이유는 광각 카메라가 긴 변 기준으로는 실제보다 좋게",
        "보이기 때문입니다.",
        "",
        "⚠️ **선명도는 하한 검사용이며 순위 지표가 아닙니다.** 실측에서",
        "320×240 지점이 720×480 지점보다 값이 높게 나왔습니다 — 저해상도일수록",
        "압축 잡음이 에지로 잡히기 때문입니다. 값이 클수록 좋다고 읽지 마십시오.",
        "판정은 사실상 **해상도**가 가릅니다.",
        "",
        "## 다음 단계",
        "",
        "1. 표본 이미지를 눈으로 확인해 판정을 검증합니다.",
        "2. 「가능성 있음」·「조건부」 지점부터 시범 라벨링합니다.",
        "   `python scripts/label_road_defects.py --block <지점 ID>`",
        "3. 시범 라벨링으로 **박스를 그을 수 있는지** 확인한 뒤 본격 투입 여부를",
        "   결정합니다 — 그을 수 없다면 노면 전용 촬영 도입을 검토해야 합니다.",
        "",
        "끝.",
    ]
    out = OUT_DIR / "report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n보고서: {out}")


if __name__ == "__main__":
    main()
