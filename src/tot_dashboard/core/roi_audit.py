"""ROI 타당성 진단 — 「그렸다」와 「제대로 그렸다」를 가른다 (2026-08-23 신설).

## 왜 필요한가

저장 검증(``cameras.validate_roi``)은 **「꼭짓점 3개 이상인 다각형이 하나는
있는가」만** 본다. 그것만 통과하면 화면 구석의 손톱만 한 삼각형도, 스스로
꼬인 다각형도, 도로가 아닌 하늘을 가리키는 영역도 그대로 저장된다.

2026-08-22 전까지는 그래도 **큰 문제가 아니었다** — 저장된 ROI 를 판정
로직이 아예 읽지 않았기 때문이다(그 자체가 결함이라 함께 고쳤다). 이제는
ROI 가 **실제 판정에 쓰이므로, 잘못 그린 ROI 는 곧바로 오판이 된다.**

  * 정체 감시 구역을 보도 위에 그리면 → 차량 0대 → 「상시 원활」
  * 도로 ROI 를 하늘에 그리면 → 물 면적 0 → 「침수 없음」

**「안 보인다」와 「없다」가 같아 보이는** 종류의 결함이라, 사람이 눈치채기
가장 어렵다.

## 무엇을 진단하는가

기하 검사(모델·스트림 불필요)
    비었는가 · 찌그러졌는가(면적 0·한 줄) · 스스로 꼬였는가 · 너무 작은가 ·
    사실상 화면 전체인가 · 저장 당시 해상도와 지금 스트림이 크게 다른가.

교통량 검사(선택, 차량 검출기 필요)
    ⚠️ **「도로 위인가」를 직접 아는 방법은 없다.** 대신 **차량이 지나가는
    곳이 도로다** — 이미 있는 차량 검출기로 표본 프레임을 훑어, 그 ROI 안에
    차량이 한 대도 안 지나갔다면 도로가 아닐 가능성이 높다고 **알린다.**

## 무엇을 하지 않는가

⚠️ **저장을 막지 않는다.** 여기서 나오는 것은 전부 「확인해 보라」는 신호이지
판정이 아니다 — 실제로 옳은 ROI 인데 이 검사에 걸리는 경우가 있다(예: 왕복
2차로의 한쪽 차선만 재려고 일부러 좁게 그린 구역). **막으면 정당한 설정을
못 하게 된다.**
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("urbanguard.roi_audit")

# --- 심각도 ------------------------------------------------------------------
ERROR = "error"      # 이대로면 판정이 확실히 틀어진다
WARN = "warn"        # 틀렸을 가능성이 높다 — 사람이 봐야 한다
INFO = "info"        # 알아 두면 좋은 것

# --- 문턱값 ------------------------------------------------------------------
#
# ⚠️ 아래 숫자는 **전부 개발사 판단**이다. 외부 기준에서 가져온 값이 아니다.
#   「이 값을 넘으면 틀렸다」가 아니라 「이 정도면 사람이 한 번 봐야 한다」는
#   뜻이며, 현장 확인 뒤 조정해야 한다.
TINY_AREA_RATIO = 0.01      # 프레임의 1% 미만 — 손이 미끄러졌을 가능성
HUGE_AREA_RATIO = 0.95      # 사실상 화면 전체 — 거를 의도가 없어 보인다
FRAME_MISMATCH_RATIO = 1.5  # 저장 해상도와 현재 해상도가 1.5배 이상 차이


@dataclass
class Finding:
    """진단 한 건."""
    severity: str
    code: str
    shape: str
    message: str

    def __str__(self) -> str:  # 로그·CLI 출력용
        return f"[{self.severity}] {self.shape}: {self.message}"


@dataclass
class RoiAudit:
    camera_id: str
    domain: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity in (ERROR, WARN) for f in self.findings)

    @property
    def worst(self) -> str:
        for level in (ERROR, WARN, INFO):
            if any(f.severity == level for f in self.findings):
                return level
        return ""


# --- 기하 도우미 --------------------------------------------------------------
def polygon_area(poly) -> float:
    """신발끈 공식. 방향과 무관하게 **절댓값**을 돌려준다."""
    pts = _normalize_ring(poly)
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _cross(o, p, q) -> float:
    return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])


def _seg_intersect(a, b, c, d) -> bool:
    """선분 ab 와 cd 가 (끝점 공유가 아닌 곳에서) 진짜로 교차하는가."""
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


# 같은 점으로 볼 픽셀 거리. ROI 편집기가 시작점 근처를 다시 클릭해 다각형을
# 닫으면 첫 점과 끝 점이 1~3px 어긋난 채 저장된다 — 실제 저장값에서 확인했다
# (2026-08-23, SEOUL-207·SEOUL-920).
CLOSE_EPS_PX = 8.0


def _normalize_ring(poly) -> list[tuple[float, float]]:
    """꼭짓점 목록을 **닫힘 중복 없는 고리**로 정리한다.

    ⚠️ 이 정리를 안 하면 꼬임 판별이 오탐한다. 다각형은 암묵적으로 닫혀
    있는데(마지막→첫 점), 편집기가 시작점 근처를 다시 찍어 닫으면 길이
    1~3px 짜리 변이 하나 더 생긴다. 그 짧은 변을 사이에 둔 두 변은 서로
    **인접하지 않은 것으로 취급**되어 교차 검사를 받고, 거의 같은 점에서
    만나므로 「꼬였다」로 잘못 잡힌다 — 실제로 정상 ROI 2건이 그렇게 잡혔다.
    """
    pts = [(float(q[0]), float(q[1])) for q in (poly or []) if len(q) >= 2]
    # 연속 중복 제거
    out: list[tuple[float, float]] = []
    for q in pts:
        if not out or (abs(q[0] - out[-1][0]) > CLOSE_EPS_PX
                       or abs(q[1] - out[-1][1]) > CLOSE_EPS_PX):
            out.append(q)
    # 끝점이 첫점과 사실상 같으면(닫음용 중복) 떼어 낸다
    while len(out) >= 2 and (abs(out[-1][0] - out[0][0]) <= CLOSE_EPS_PX
                             and abs(out[-1][1] - out[0][1]) <= CLOSE_EPS_PX):
        out.pop()
    return out


def is_self_intersecting(poly) -> bool:
    """스스로 꼬인 다각형인가.

    꼬인 다각형은 ``cv2.fillPoly`` 가 채우는 모양과 사람이 화면에서 본 모양이
    달라진다 — **화면과 판정이 다른 영역을 가리키게 된다.**
    """
    pts = _normalize_ring(poly)
    n = len(pts)
    if n < 4:
        return False
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            # 이웃한 변은 끝점을 공유하므로 건너뛴다.
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _seg_intersect(a, b, pts[j], pts[(j + 1) % n]):
                return True
    return False


# --- 진단 --------------------------------------------------------------------
def audit_shapes(camera_id: str, domain: str, roi: dict,
                 *, current_wh: tuple[int, int] | None = None) -> RoiAudit:
    """저장된 ROI 하나를 기하학적으로 진단한다(모델·스트림 불필요).

    :param roi: ``cameras.roi_of()`` 결과
        (``{"frame_width", "frame_height", "shapes"}``).
    :param current_wh: 지금 스트림의 해상도를 알면 넘긴다 — 저장 당시와 크게
        다르면 좌표 보정이 부정확할 수 있음을 알린다.
    """
    from . import cameras as C

    out = RoiAudit(camera_id=camera_id, domain=domain)
    fw = int(roi.get("frame_width") or 0)
    fh = int(roi.get("frame_height") or 0)
    shapes = roi.get("shapes") or {}
    frame_area = float(fw * fh) if fw > 0 and fh > 0 else 0.0

    has_any = any(v for v in shapes.values())
    if not has_any:
        # 아예 안 그린 것은 「잘못 그린 것」과 다르다 — 필수 도형만 짚는다.
        need = C.REQUIRED_SHAPE.get(domain)
        if need:
            label = next((lb for k, lb, _t, _r in C.ROI_SHAPES.get(domain, [])
                          if k == need), need)
            out.findings.append(Finding(
                ERROR, "MISSING", label,
                "필수 구역인데 비어 있습니다 — 판정이 화면 전체를 씁니다."))
        return out

    if frame_area <= 0:
        out.findings.append(Finding(
            WARN, "NO_FRAME_SIZE", "-",
            "저장 당시 정지영상 크기를 알 수 없습니다 — 해상도가 다른 "
            "스트림에서 좌표를 보정할 수 없습니다."))

    required = C.REQUIRED_SHAPE.get(domain)
    for key, label, kind, req in C.ROI_SHAPES.get(domain, []):
        raw = shapes.get(key) or []
        if kind == "line":
            if raw and len(raw) != 2:
                out.findings.append(Finding(
                    ERROR, "BAD_LINE", label,
                    f"점 2개여야 하는데 {len(raw)}개입니다."))
            continue

        if kind == "arrows":
            # ⚠️ 2026-08-26 — 여기서 손대지 않으면 화살표가 아래
            # ``len(p) >= 3`` 필터(폴리곤 전용)에 걸려 **진단이 침묵한 채로
            # 통째로 사라진다**(Phase 4 계획에서 미리 짚은 함정). 화살표는
            # 점이 정확히 2개라 폴리곤 검사를 그대로 못 쓴다.
            arrows = [a for a in raw if a and len(a) == 2]
            if not arrows:
                if req or key == required:
                    out.findings.append(Finding(
                        ERROR, "MISSING", label,
                        "필수 구역인데 비어 있습니다 — 판정이 화면 전체를 씁니다."))
                continue
            for idx, arr in enumerate(arrows):
                tag = f"{label}[{idx + 1}]" if len(arrows) > 1 else label
                (x1, y1), (x2, y2) = arr
                if x1 == x2 and y1 == y2:
                    out.findings.append(Finding(
                        ERROR, "DEGENERATE", tag,
                        "시작점과 끝점이 같아 방향을 알 수 없습니다."))
                    continue
                if frame_area > 0 and (
                        (x1 < 0 or x1 > fw or y1 < 0 or y1 > fh)
                        and (x2 < 0 or x2 > fw or y2 < 0 or y2 > fh)):
                    out.findings.append(Finding(
                        ERROR, "OUT_OF_FRAME", tag, "화살표가 화면 밖에 있습니다."))
            continue

        polys = [p for p in raw if p and len(p) >= 3]
        if not polys:
            if req or key == required:
                out.findings.append(Finding(
                    ERROR, "MISSING", label,
                    "필수 구역인데 비어 있습니다 — 판정이 화면 전체를 씁니다."))
            continue

        total = 0.0
        for idx, poly in enumerate(polys):
            area = polygon_area(poly)
            total += area
            tag = f"{label}[{idx + 1}]" if len(polys) > 1 else label
            # ⚠️ **꼬임을 면적보다 먼저 본다.** 꼬인 다각형은 신발끈 면적이
            #   무의미하다 — 대칭 나비넥타이는 두 잎이 상쇄돼 면적이 정확히
            #   0으로 나온다. 면적을 먼저 보면 「찌그러졌다」로 잡혀 **진짜
            #   원인(꼬임)을 못 알려 준다.**
            if is_self_intersecting(poly):
                out.findings.append(Finding(
                    ERROR, "SELF_INTERSECTING", tag,
                    "다각형이 스스로 꼬였습니다 — 화면에서 본 모양과 실제 "
                    "판정 영역이 달라집니다."))
                continue
            if area <= 1.0:
                out.findings.append(Finding(
                    ERROR, "DEGENERATE", tag,
                    "면적이 사실상 0입니다(점이 한 줄로 늘어섰거나 겹쳤습니다)."))
                continue
            if frame_area > 0:
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                if max(xs) < 0 or min(xs) > fw or max(ys) < 0 or min(ys) > fh:
                    out.findings.append(Finding(
                        ERROR, "OUT_OF_FRAME", tag,
                        "영역이 화면 밖에 있습니다."))

        if frame_area > 0 and total > 0:
            ratio = total / frame_area
            if ratio < TINY_AREA_RATIO:
                out.findings.append(Finding(
                    WARN, "TINY", label,
                    f"화면의 {ratio * 100:.2f}% 만 덮습니다 — 의도한 크기가 "
                    "맞는지 확인하십시오."))
            elif ratio > HUGE_AREA_RATIO:
                out.findings.append(Finding(
                    INFO, "HUGE", label,
                    f"화면의 {ratio * 100:.0f}% 를 덮습니다 — 사실상 화면 "
                    "전체라 거르는 효과가 거의 없습니다."))

    if current_wh and frame_area > 0:
        cw, ch = current_wh
        if cw > 0 and ch > 0:
            r = max(cw / fw, fw / cw, ch / fh, fh / ch)
            if r >= FRAME_MISMATCH_RATIO:
                out.findings.append(Finding(
                    WARN, "FRAME_MISMATCH", "-",
                    f"저장 당시 {fw}x{fh} 인데 지금 스트림은 {cw}x{ch} 입니다 "
                    "— 비율이 다르면 좌표 보정이 부정확할 수 있습니다."))
    return out


def audit_traffic_overlap(polygons, vehicle_boxes, *,
                          label: str = "정체 감시 구역") -> Finding | None:
    """차량이 그 구역을 지나가는지로 「도로 위인가」를 **추정**한다.

    ⚠️ **도로인지 직접 아는 방법은 없다.** 차량이 다니는 곳이 도로라는
    간접 근거를 쓴다. 그래서 결과는 ``WARN`` 이지 ``ERROR`` 가 아니다 —
    한산한 시간대에 표본을 뜨면 정상 ROI 도 0대가 나온다.

    :param vehicle_boxes: ``[(x1, y1, x2, y2), ...]`` — 표본 프레임 전체에서
        모은 차량 박스. 판정 기준점은 판정 로직과 같은 **하단 중심**이다.
    """
    from ..common.roi import point_in_polygons

    if not polygons or not vehicle_boxes:
        return None
    inside = sum(1 for b in vehicle_boxes
                 if point_in_polygons(((b[0] + b[2]) / 2.0, float(b[3])), polygons))
    if inside == 0:
        return Finding(
            WARN, "NO_TRAFFIC", label,
            f"표본에서 차량 {len(vehicle_boxes)}대를 봤지만 이 구역을 지난 "
            "차량이 한 대도 없습니다 — 도로가 아닌 곳에 그렸을 수 "
            "있습니다(한산한 시간대라면 정상일 수 있으니 다시 확인하십시오).")
    ratio = inside / len(vehicle_boxes)
    return Finding(
        INFO, "TRAFFIC_OK", label,
        f"표본 차량 {len(vehicle_boxes)}대 중 {inside}대"
        f"({ratio * 100:.0f}%)가 이 구역을 지났습니다.")


def audit_camera(db, camera_id: str, domain: str, *,
                 current_wh: tuple[int, int] | None = None) -> RoiAudit:
    """DB에서 ROI 를 읽어 진단한다."""
    from . import cameras as C

    return audit_shapes(camera_id, domain, C.roi_of(db, camera_id, domain),
                        current_wh=current_wh)


def audit_all(db, *, domains: list[str] | None = None) -> list[RoiAudit]:
    """등록된 모든 카메라·도메인의 ROI 를 진단한다.

    **탐지 지정이 켜진 도메인만** 본다 — 안 쓰는 도메인의 보관용 ROI 까지
    경고하면 목록이 노이즈로 가득 차 정작 볼 것을 못 본다.
    """
    from . import cameras as C

    want = set(domains) if domains else set(C.ROI_SHAPES)
    out: list[RoiAudit] = []
    for cam in C.list_all(db):
        dm = C.domain_map(cam)
        for dom in C.ROI_SHAPES:
            if dom not in want or not dm.get(dom, {}).get("enabled"):
                continue
            out.append(audit_camera(db, cam.id, dom))
    return out
