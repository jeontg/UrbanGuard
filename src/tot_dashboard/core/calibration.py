"""지점별 캘리브레이션 — 화면 단위를 실제 물리 단위로 바꾼다.

왜 필요한가
    국내외 안전 기준은 전부 **물리 단위**로 되어 있습니다.

      · 침수  — 침수심 **cm** (행안부 지하차도 5cm · NWS/FEMA 15·30cm)
      · 인파  — **명/㎡** (국내 3·4·5명 · Fruin LOS · 영국 Green Guide)
      · 노면  — **구간 단위 지수** (ASTM D6433 PCI · 서울시 SPI)

    그런데 우리가 재는 것은 전부 **화면 단위**입니다 — 면적 비율(%), 격자
    점유율(%), 탐지 개수. 그래서 기준이 있어도 그대로 옮길 수 없었습니다
    (docs/202608151713/threshold_rationale.md).

    이 모듈은 **그 간극을 지점별 캘리브레이션으로 메웁니다.**

설계 원칙 — 보정하지 않았으면 보정한 척하지 않는다
    캘리브레이션이 없으면 모든 변환 함수가 **``None`` 을 돌려줍니다.**
    0 이나 추정값을 주지 않습니다. 화면은 그 ``None`` 을 「미보정」으로
    표시해야 하며, **보정 안 된 지점의 「3.2명/㎡」가 보정된 것처럼 보이는
    상태를 만들면 안 됩니다.** 지금까지 걷어내 온 「거짓말하는 화면」과
    같은 부류이기 때문입니다.

저장 위치
    ``camera_domains.config`` (JSONB) 의 ``calibration`` 키. 도메인마다 필요한
    보정이 달라 카메라×도메인 단위로 둡니다. 새 테이블이 필요 없습니다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger("urbanguard.calibration")

# --- 국내외 기준 (docs/202608151713/threshold_rationale.md) -------------------
#
# 여기 숫자는 **외부 기준에서 그대로 가져온 값**이다. 자체 판단이 아니다.
# 고칠 일이 있으면 근거 문서를 함께 고쳐야 한다.

# 인파 — 1㎡당 명. [외부] 국내 인파관리 체계 + 국제 관측(4~5명에서 위험 급증,
# 5명 이상 압사 임계)이 같은 지점을 가리킨다.
CROWD_M2_CAUTION = 3.0     # 주의
CROWD_M2_ALERT = 4.0       # 경계 — 영국 이동 대기열 한계와도 일치
CROWD_M2_CRITICAL = 5.0    # 심각 — 국제 압사 임계

# 침수 — 침수심 cm. [외부]
FLOOD_CM_CONTROL = 5.0     # 국내 지하차도 통제 기준 (행안부, 15→5cm 강화)
FLOOD_CM_TRACTION = 15.0   # 차량 접지력 상실 시작 (NWS/FEMA)
FLOOD_CM_FLOAT = 30.0      # 소형차 부유 시작 (NWS/FEMA)

# 노면 — 100m 당 손상 건수. ⚠️ [자체] — 이 구간만은 외부 기준을 찾지 못했다.
# PCI·SPI 는 균열률·소성변형·평탄성으로 산출하며 「건수」로 등급을 나누는
# 공표 기준은 확인되지 않았다. 구간 단위로 바꾼 것 자체가 PCI 방식에
# 가까워진 것이며, 경계값은 여전히 도로관리 부서 협의 대상이다.
ROAD_PER100M_WATCH = 1.0
ROAD_PER100M_REPAIR = 3.0
ROAD_PER100M_URGENT = 6.0

CALIBRATION_KEY = "calibration"


# --- 지면 평면 (호모그래피) --------------------------------------------------
@dataclass(frozen=True)
class GroundPlane:
    """화면 좌표 → 실제 바닥 좌표(m).

    카메라가 비스듬히 내려다보므로 화면의 같은 픽셀 넓이가 실제로는 위쪽일수록
    넓은 면적입니다. 네 점의 대응만 알면 그 왜곡을 풀 수 있습니다.

    ``image_points`` 는 화면 픽셀 좌표 4점, ``world_points`` 는 그 네 점의
    실제 바닥 좌표(미터) 4점입니다. 현장에서 **직사각형 하나만 재면** 됩니다 —
    횡단보도, 보도블록 몇 장, 주차 구획 등.
    """

    image_points: list[list[float]]
    world_points: list[list[float]]

    def matrix(self):
        """투시 변환 행렬. 계산 실패 시 None."""
        try:
            import cv2
            import numpy as np

            src = np.array(self.image_points, dtype="float32")
            dst = np.array(self.world_points, dtype="float32")
            if src.shape != (4, 2) or dst.shape != (4, 2):
                return None
            return cv2.getPerspectiveTransform(src, dst)
        except Exception:  # noqa: BLE001
            log.exception("호모그래피 계산 실패")
            return None

    def to_world(self, points: Sequence[Sequence[float]]):
        """화면 좌표들을 바닥 좌표(m)로. 실패하면 None."""
        m = self.matrix()
        if m is None or not points:
            return None
        try:
            import cv2
            import numpy as np

            arr = np.array([[list(p) for p in points]], dtype="float32")
            return cv2.perspectiveTransform(arr, m)[0]
        except Exception:  # noqa: BLE001
            log.exception("좌표 변환 실패")
            return None

    def area_m2(self, polygon_px: Sequence[Sequence[float]]) -> float | None:
        """화면 폴리곤이 실제로 덮는 바닥 면적(㎡). 실패하면 None."""
        if not polygon_px or len(polygon_px) < 3:
            return None
        world = self.to_world(polygon_px)
        if world is None:
            return None
        # 신발끈 공식
        area = 0.0
        n = len(world)
        for i in range(n):
            x1, y1 = float(world[i][0]), float(world[i][1])
            x2, y2 = float(world[(i + 1) % n][0]), float(world[(i + 1) % n][1])
            area += x1 * y2 - x2 * y1
        area = abs(area) / 2.0
        return area if area > 0 else None

    def meters_per_pixel(self, at_point: Sequence[float] | None = None) -> float | None:
        """대략의 화면 1픽셀당 실제 길이(m).

        ⚠️ **위치마다 다릅니다.** 원근 때문에 화면 위쪽 1픽셀이 아래쪽보다
        훨씬 깁니다. 픽셀 속도 임계값을 지점별로 보정할 때의 **참고값**일 뿐,
        정확한 속도가 필요하면 :meth:`to_world` 로 두 점을 변환해 재십시오.
        """
        m = self.matrix()
        if m is None:
            return None
        px = list(at_point) if at_point else self._center_px()
        world = self.to_world([px, [px[0] + 1.0, px[1]]])
        if world is None:
            return None
        dx = float(world[1][0]) - float(world[0][0])
        dy = float(world[1][1]) - float(world[0][1])
        d = (dx * dx + dy * dy) ** 0.5
        return d if d > 0 else None

    def _center_px(self) -> list[float]:
        xs = [p[0] for p in self.image_points]
        ys = [p[1] for p in self.image_points]
        return [sum(xs) / len(xs), sum(ys) / len(ys)]


# --- 침수심 대응표 -----------------------------------------------------------
@dataclass(frozen=True)
class DepthTable:
    """면적 비율 → 침수심(cm) 대응표.

    현장에서 **수위표(스타프)를 화면 안에 세우고**, 물이 찰 때 「면적비 얼마일
    때 눈금 몇 cm」를 몇 점 기록하면 만들어집니다. 점 사이는 선형 보간합니다.

    ``points`` = [[면적비, 침수심cm], ...] — 면적비 오름차순이 아니어도 됩니다.
    """

    points: list[list[float]]

    def depth_cm(self, ratio: float) -> float | None:
        """면적비에 해당하는 침수심 추정값(cm). 점이 2개 미만이면 None."""
        pts = sorted(((float(r), float(d)) for r, d in self.points
                      if r is not None and d is not None),
                     key=lambda p: p[0])
        if len(pts) < 2:
            return None
        if ratio <= pts[0][0]:
            return pts[0][1]
        if ratio >= pts[-1][0]:
            # ⚠️ 관측 범위 밖은 **외삽하지 않습니다.** 마지막 관측값으로
            #    묶어 두는 편이 안전합니다 — 외삽한 깊이로 통제를 권고하면
            #    근거 없는 숫자가 의사결정에 들어갑니다.
            return pts[-1][1]
        for (r1, d1), (r2, d2) in zip(pts, pts[1:]):
            if r1 <= ratio <= r2:
                if r2 == r1:
                    return d2
                t = (ratio - r1) / (r2 - r1)
                return d1 + t * (d2 - d1)
        return None

    def saturated(self, ratio: float) -> bool:
        """관측 범위를 넘어선 값인가. 화면이 「이 이상」으로 표시하도록."""
        pts = sorted(float(r) for r, _d in self.points if r is not None)
        return bool(pts) and ratio > pts[-1]


# --- 노면 구간 ---------------------------------------------------------------
@dataclass(frozen=True)
class RoadSection:
    """이 CCTV 한 대가 담당하는 도로 구간 길이(m).

    PCI·SPI 는 **구간 단위**로 평가합니다. 구간 길이를 알면 「탐지 3건」을
    「100m당 몇 건」으로 바꿀 수 있어, 지점끼리 비교가 되고 보수 우선순위의
    근거로 쓸 수 있습니다. 개수만으로는 긴 구간이 항상 불리합니다.
    """

    length_m: float

    def per_100m(self, count: int) -> float | None:
        if self.length_m <= 0:
            return None
        return count / self.length_m * 100.0


# --- 읽기·쓰기 ---------------------------------------------------------------
@dataclass(frozen=True)
class Calibration:
    ground: GroundPlane | None = None
    depth: DepthTable | None = None
    section: RoadSection | None = None

    @property
    def any(self) -> bool:
        return any((self.ground, self.depth, self.section))

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        if self.ground:
            out["ground"] = {"image_points": self.ground.image_points,
                             "world_points": self.ground.world_points}
        if self.depth:
            out["depth"] = {"points": self.depth.points}
        if self.section:
            out["section"] = {"length_m": self.section.length_m}
        return out


def from_dict(data: dict | None) -> Calibration:
    """저장된 값에서 캘리브레이션을 만든다. 깨진 값은 조용히 버린다.

    설정이 잘못돼 있다고 탐지가 멈추면 안 되므로, 항목 단위로 무시한다.
    """
    if not isinstance(data, dict):
        return Calibration()
    ground = depth = section = None

    g = data.get("ground")
    if isinstance(g, dict):
        ip, wp = g.get("image_points"), g.get("world_points")
        if (isinstance(ip, list) and isinstance(wp, list)
                and len(ip) == 4 and len(wp) == 4):
            try:
                ground = GroundPlane(
                    image_points=[[float(x), float(y)] for x, y in ip],
                    world_points=[[float(x), float(y)] for x, y in wp])
            except Exception:  # noqa: BLE001
                log.warning("지면 캘리브레이션 형식 오류 — 무시합니다")

    d = data.get("depth")
    if isinstance(d, dict) and isinstance(d.get("points"), list):
        try:
            pts = [[float(r), float(c)] for r, c in d["points"]]
            if len(pts) >= 2:
                depth = DepthTable(points=pts)
        except Exception:  # noqa: BLE001
            log.warning("침수심 대응표 형식 오류 — 무시합니다")

    s = data.get("section")
    if isinstance(s, dict):
        try:
            length = float(s.get("length_m") or 0)
            if length > 0:
                section = RoadSection(length_m=length)
        except Exception:  # noqa: BLE001
            log.warning("구간 길이 형식 오류 — 무시합니다")

    return Calibration(ground=ground, depth=depth, section=section)


def of(cam, domain: str) -> Calibration:
    """카메라×도메인의 캘리브레이션. 없으면 빈 것."""
    row = cam.domain_row(domain) if cam is not None else None
    cfg = (row.config if row else None) or {}
    return from_dict(cfg.get(CALIBRATION_KEY))


# --- 판정 --------------------------------------------------------------------
def crowd_per_m2(cal: Calibration, person_count: int,
                 area_polygon_px: Sequence[Sequence[float]] | None) -> float | None:
    """인파 밀도(명/㎡). **보정 안 됐으면 None.**"""
    if cal.ground is None or not area_polygon_px:
        return None
    area = cal.ground.area_m2(area_polygon_px)
    if not area:
        return None
    return person_count / area


def traffic_speed_kmh(cal: Calibration, p0_px: Sequence[float],
                      p1_px: Sequence[float], dt_sec: float) -> float | None:
    """화면 두 점(픽셀) 사이의 실제 이동거리(m)로 속도(km/h)를 낸다.
    **보정 안 됐으면 None.**

    2026-08-26 — 예전에는 ``service/runner.py``가 ``block.get("calib")``를
    읽었는데, 그 키를 **채우는 코드가 어디에도 없어** 항상 기본값
    ``mpp=0.06``(픽셀속도×0.216 고정계수)으로 떨어졌다. 화면·이벤트·
    보고서에 나가는 「평균속도 N km/h」가 전부 이 가짜 값이었다
    (`docs/202608260842/` 계획에서 확인).

    ⚠️ :meth:`GroundPlane.meters_per_pixel`을 쓰지 않는다 — 그 메서드
    자체가 「위치마다 다르다, 정확한 속도가 필요하면 to_world로 두 점을
    변환해 재라」고 경고한다(calibration.py:128-131). 여기서는 그 경고를
    그대로 따라 **두 점을 각각 세계 좌표로 옮긴 뒤 실제 거리를 잰다** —
    원근 왜곡을 실제로 푸는 유일한 방법이다.
    """
    if cal.ground is None or dt_sec <= 0:
        return None
    world = cal.ground.to_world([list(p0_px), list(p1_px)])
    if world is None:
        return None
    dx = float(world[1][0]) - float(world[0][0])
    dy = float(world[1][1]) - float(world[0][1])
    dist_m = (dx * dx + dy * dy) ** 0.5
    return dist_m / dt_sec * 3.6


UNCALIBRATED = "미보정"


def _level_from_table(db, domain: str, value: float) -> str | None:
    """등급 구간 표에서 단계를 찾는다. 표가 비었거나 실패하면 ``None``.

    ⚠️ **실패를 「관심」으로 바꾸면 안 된다.** 표를 못 읽었는데 「관심」이라
    답하면, 위험한 상황이 안전하게 보인다. 호출부가 코드 기본값으로
    되돌아가도록 ``None`` 을 준다.
    """
    if db is None:
        return None
    try:
        from . import vocabulary as V
        rows = V.thresholds(db, domain)      # 높은 등급부터
        if not rows:
            return None
        for code, minv, _unit in rows:
            if value >= minv:
                return V.label_of(db, code)
        # ⚠️ 바닥 등급은 도메인마다 다르다. 노면은 「관심」이 아니라
        #    **「양호」**다. 여기서 「관심」을 돌려주면 노면 화면에 위험등급
        #    어휘가 섞여 나온다.
        return V.label_of(db, V.BASE_LEVEL_BY_DOMAIN.get(domain, V.BASE_LEVEL))
    except Exception:  # noqa: BLE001
        return None


def crowd_level(per_m2: float | None, *, db=None) -> str:
    """국내외 기준에 따른 단계. 보정 안 됐으면 「미보정」.

    ``db`` 를 주면 **등급 구간 표를 먼저 본다**(S-95 에서 기관이 조정 가능).
    표가 비어 있으면 아래 코드 기본값으로 되돌아간다.
    """
    if per_m2 is None:
        return UNCALIBRATED
    got = _level_from_table(db, "crowd", per_m2)
    if got is not None:
        return got
    if per_m2 >= CROWD_M2_CRITICAL:
        return "심각"
    if per_m2 >= CROWD_M2_ALERT:
        return "경계"
    if per_m2 >= CROWD_M2_CAUTION:
        return "주의"
    return "관심"


def flood_depth_cm(cal: Calibration, water_ratio: float) -> float | None:
    """면적비 → 추정 침수심(cm). **보정 안 됐으면 None.**"""
    if cal.depth is None:
        return None
    return cal.depth.depth_cm(water_ratio)


def flood_level(depth_cm: float | None, *, db=None) -> str:
    """국내외 침수심 기준에 따른 단계. 보정 안 됐으면 「미보정」.

    ⚠️ **2026-08-19 어휘를 4등급으로 통일했다.** 이전에는
    「통제 권고·위험·주의·관심」을 돌려줬는데, 인파는 「관심·주의·경계·심각」
    이라 **같은 상황판에 서로 다른 말이 떴다.** 관제요원이 어느 쪽이 더
    급한지 알 수 없다는 것이 문제였다.

    바뀐 대응은 이렇다 — 숫자 기준은 그대로다.

    ==========  ============  =========================
    침수심       이전           지금
    ==========  ============  =========================
    30cm 이상    통제 권고      **심각**
    15cm 이상    위험           **경계**
    5cm 이상     주의           주의
    그 아래       관심           관심
    ==========  ============  =========================

    ``db`` 를 주면 등급 구간 표를 먼저 본다(S-95 에서 기관이 조정 가능).
    """
    if depth_cm is None:
        return UNCALIBRATED
    got = _level_from_table(db, "flood", depth_cm)
    if got is not None:
        return got
    if depth_cm >= FLOOD_CM_FLOAT:
        return "심각"
    if depth_cm >= FLOOD_CM_TRACTION:
        return "경계"
    if depth_cm >= FLOOD_CM_CONTROL:
        return "주의"
    return "관심"


def road_per_100m(cal: Calibration, defect_count: int) -> float | None:
    """손상 밀도(건/100m). **구간 길이를 모르면 None.**"""
    if cal.section is None:
        return None
    return cal.section.per_100m(defect_count)


def road_level(per_100m: float | None, *, db=None) -> str:
    """노면 **정비 등급**. 구간 길이를 안 넣었으면 「미보정」.

    ``db`` 를 주면 **등급 구간 표를 먼저 본다**(S-95 에서 기관이 조정 가능).
    표가 없거나 읽지 못하면 아래 코드 기본값으로 되돌아간다.

    ⚠️ **위험등급이 아니다.** 「긴급」은 「지금 통제하라」가 아니라 **「빨리
    보수하라」**다. 침수 「심각」과 같은 척도에 올리면 안 된다 —
    :data:`~.vocabulary.KIND_MAINTENANCE` 참고.

    ⚠️ **「미보정」을 「양호」로 바꾸면 안 된다.** 구간 길이를 안 넣어 판정을
    못 한 것인데 「양호」라고 답하면 **점검이 끝난 것처럼 보인다.**
    """
    if per_100m is None:
        return "미보정"
    got = _level_from_table(db, "road", per_100m)
    if got is not None:
        return got
    if per_100m >= ROAD_PER100M_URGENT:
        return "긴급"
    if per_100m >= ROAD_PER100M_REPAIR:
        return "보수 필요"
    if per_100m >= ROAD_PER100M_WATCH:
        return "관찰"
    return "양호"


# --- 입력 검증 ---------------------------------------------------------------
def validate(data: dict) -> list[str]:
    """저장 전 검사. 오류 문구 목록(비면 통과)."""
    errors: list[str] = []

    g = data.get("ground")
    if g:
        ip, wp = g.get("image_points"), g.get("world_points")
        if not (isinstance(ip, list) and len(ip) == 4):
            errors.append("지면 보정: 화면 좌표는 4점이어야 합니다.")
        if not (isinstance(wp, list) and len(wp) == 4):
            errors.append("지면 보정: 실제 좌표(m)는 4점이어야 합니다.")
        if not errors:
            plane = GroundPlane(image_points=[[float(a), float(b)] for a, b in ip],
                                world_points=[[float(a), float(b)] for a, b in wp])
            if plane.matrix() is None:
                errors.append("지면 보정: 네 점으로 평면을 풀 수 없습니다. "
                              "세 점이 일직선이 아닌지 확인하십시오.")
            else:
                # 실제 좌표가 한 점에 몰려 있으면 면적이 0이 되어 나눗셈이 터진다.
                area = plane.area_m2([[0, 0], [1, 0], [1, 1], [0, 1]])
                if area is None:
                    errors.append("지면 보정: 실제 좌표가 면적을 이루지 않습니다.")

    d = data.get("depth")
    if d:
        pts = d.get("points")
        if not (isinstance(pts, list) and len(pts) >= 2):
            errors.append("침수심 대응표: 관측점이 2개 이상 필요합니다.")
        else:
            try:
                vals = [(float(r), float(c)) for r, c in pts]
            except Exception:  # noqa: BLE001
                errors.append("침수심 대응표: 숫자만 입력하십시오.")
                vals = []
            for r, c in vals:
                if not 0.0 <= r <= 1.0:
                    errors.append(f"침수심 대응표: 면적비는 0~1 사이여야 합니다 ({r}).")
                if c < 0:
                    errors.append(f"침수심 대응표: 침수심은 0 이상이어야 합니다 ({c}).")
            if len({r for r, _ in vals}) < len(vals):
                errors.append("침수심 대응표: 같은 면적비가 두 번 들어 있습니다.")

    s = data.get("section")
    if s:
        try:
            length = float(s.get("length_m") or 0)
        except Exception:  # noqa: BLE001
            errors.append("구간 길이: 숫자를 입력하십시오.")
            length = 0
        if length <= 0:
            errors.append("구간 길이: 0보다 커야 합니다.")
        elif length > 10000:
            errors.append("구간 길이: 10km 를 넘습니다. 단위(m)를 확인하십시오.")

    return errors


def save(db, cam, domain: str, data: dict) -> list[str]:
    """캘리브레이션을 저장한다. 오류 문구 목록(비면 성공).

    ``camera_domains.config`` 의 다른 항목은 건드리지 않는다.
    """
    from .models import CameraDomain

    errors = validate(data)
    if errors:
        return errors

    row = cam.domain_row(domain)
    if row is None:
        row = CameraDomain(camera_id=cam.id, domain=domain)
        db.add(row)
        cam.domains.append(row)
    cfg = dict(row.config or {})
    cleaned = from_dict(data).to_dict()
    if cleaned:
        cfg[CALIBRATION_KEY] = cleaned
    else:
        cfg.pop(CALIBRATION_KEY, None)
    # JSONB 는 딕셔너리를 통째로 바꿔야 변경으로 인식된다.
    row.config = cfg
    db.commit()
    return []
