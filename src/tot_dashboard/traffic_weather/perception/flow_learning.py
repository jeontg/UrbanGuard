"""통행 방향 화살표 자동 생성 — Phase 4-A (2026-08-26).

## 원리

대부분의 차량은 정상 방향으로 간다. 따라서 **관측된 흐름의 다수 방향이
곧 정상 방향**이고, 역주행은 정의상 드문 이상치다.

1. 완료된 차량 트랙의 변위 벡터(시작점→끝점)를 누적한다. 변위가 작은
   (정지·주차) 트랙은 제외한다.
2. 화면을 성긴 격자로 나눠 칸마다 **원형 평균**(circular mean) 헤딩을
   낸다 — 각도는 360°에서 감기므로 산술평균을 쓰면 안 된다(350°와 10°의
   산술평균은 180°지만, 실제로는 0°에 가깝다).
3. **원형 분산이 낮으면**(단봉) 그 칸의 화살표를 제안한다.
4. **양봉이면**(왕복 차선이 한 칸에 겹침) 자동 판정하지 않고 "사람이
   그려주세요"로 표시한다 — 억지로 평균 내면 무의미한 방향이 나온다.
5. 인접 칸을 병합해 화살표 개수를 줄인다.

## ⚠️ 이 모듈은 "제안"만 한다 — 저장하지 않는다

여기서 나온 화살표는 반드시 **"검토 대기"**로 다뤄야 한다. 관리자가 확인한
것만 역주행 판정에 쓴다(``core/cameras.py``의 ``flow_arrows`` ROI에 저장하는
주체는 언제나 사람이다). 검토 안 된 방향으로 판정하면 자동 추정이 틀린
카메라에서 역주행 오탐이 조직적으로 발생한다 — 이 프로젝트의 "모르면
판정하지 않는다" 원칙(``common/roi.py``·``core/calibration.py``)과 같다.

## 라이브 파이프라인 연동은 별도 과제다

이 모듈은 **순수 함수**만 담는다 — 변위 벡터를 어디서 모으는지는 모른다.
실시간 트래커(``TrafficBehaviorTracker``)가 완료된 트랙의 변위를 어디에
누적하고, 그 누적치를 화면(``camera_roi.html``의 "자동 생성" 버튼)이 어떻게
가져오는지는 ``docs/pending_tasks.md``에 별도 항목으로 남겨 뒀다 — 카메라
ID → 살아 있는 파이프라인 인스턴스를 찾는 레지스트리가 아직 없다
(``routes_cameras.py``는 지금 DB만 본다).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

Vector = tuple[float, float, float, float]  # (x0, y0, x1, y1)


# --- 원형 통계 -----------------------------------------------------------------


def circular_mean_deg(headings_deg: Sequence[float]) -> float:
    """원형 평균(도, 0~360). 빈 목록이면 0.0.

    각도는 360°에서 감긴다 — 350°와 10°의 산술평균은 180°(정반대)지만
    실제 평균 방향은 0°(그 사이, 짧은 쪽)에 가깝다. 단위 벡터의 합으로
    구하면 이 문제가 저절로 풀린다.
    """
    if not headings_deg:
        return 0.0
    sin_sum = sum(math.sin(math.radians(h)) for h in headings_deg)
    cos_sum = sum(math.cos(math.radians(h)) for h in headings_deg)
    if sin_sum == 0.0 and cos_sum == 0.0:
        return 0.0  # 완전히 상쇄(정확히 양봉 대칭) — 평균 방향이 정의되지 않는다
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0


def circular_variance(headings_deg: Sequence[float]) -> float:
    """0(완전히 한 방향) ~ 1(사방으로 흩어짐). 표준 원형 통계 정의
    (평균 결과 벡터 길이 R을 1에서 뺀 값)이다.

    ⚠️ 양봉(왕복 차선이 한 칸에 겹침)도 산술 표준편차로는 못 잡는다 —
    180°씩 정확히 갈리면 원형 평균 자체가 무의미해지면서 분산이 1에
    가깝게 나온다. 그래서 여기 하나만으로 "단봉 vs 양봉"을 가르고,
    실제 양봉 여부는 :func:`is_bimodal`로 따로 확인한다.
    """
    if not headings_deg:
        return 1.0
    n = len(headings_deg)
    sin_sum = sum(math.sin(math.radians(h)) for h in headings_deg)
    cos_sum = sum(math.cos(math.radians(h)) for h in headings_deg)
    r = math.hypot(sin_sum, cos_sum) / n
    return max(0.0, 1.0 - r)


def circular_angle_diff_deg(a_deg: float, b_deg: float) -> float:
    """두 각도(도) 사이의 최단 각도차. 0~180."""
    d = abs(a_deg - b_deg) % 360.0
    return min(d, 360.0 - d)


def is_bimodal(headings_deg: Sequence[float]) -> bool:
    """대략 반대 방향인 두 무리로 갈리는지 — 왕복 차선이 한 칸에 겹친
    상황을 잡는다.

    구현: 각도를 두 배로 접어(0°와 180°가 같은 자리로 겹친다) 그 원형
    평균의 절반을 "축(axis)"으로 삼는다. 축을 기준으로 "그쪽"과
    "반대쪽"으로 표본을 나눠, 소수 쪽이 전체의 1/3 이상이면 양봉으로 본다.

    ⚠️ 원래 각도로 그냥 원형 평균을 내 축으로 쓰면 **정확히 반반(예:
    0°와 180°가 5:5)으로 갈린 경우** 그 평균 자체가 상쇄돼 부동소수
    오차로 임의의 값이 나온다(발견 경위: ``test_flow_learning.py``의
    양봉 시험이 이 경계에서 실패했다). 두 배로 접는 방법은 그 대칭을
    active하게 이용해 축을 안정적으로 찾는다 — 원형통계학의 표준 기법.
    """
    if len(headings_deg) < 2:
        return False
    axis = circular_mean_deg([2.0 * h for h in headings_deg]) / 2.0
    same_side = sum(1 for h in headings_deg
                    if circular_angle_diff_deg(h, axis) < 90.0)
    opposite_side = len(headings_deg) - same_side
    minority = min(same_side, opposite_side)
    # 소수 쪽이 전체의 1/3 이상이면 "두 무리"로 본다 — 소수점 이하 잡음
    # 몇 대 때문에 단봉을 양봉으로 잘못 보지 않도록 여유를 둔다.
    return minority / len(headings_deg) >= (1.0 / 3.0)


# --- 격자 집계 -----------------------------------------------------------------


@dataclass
class CellResult:
    cell: tuple[int, int]
    center: tuple[float, float]
    n: int
    headings_deg: list[float] = field(default_factory=list)
    mean_heading_deg: float | None = None
    variance: float = 1.0
    # "propose"    — 단봉, 화살표 제안 가능
    # "review"     — 표본은 있는데 양봉/분산이 커서 사람이 그려야 함
    # "insufficient" — 표본이 min_samples 미만
    status: str = "insufficient"


# [자체] — 부산 실측으로 정한 값이 아니다. 현장 확인 후 조정 대상.
DEFAULT_CELL_PX = 150.0
DEFAULT_MIN_SAMPLES = 5
DEFAULT_VARIANCE_THRESHOLD = 0.5
DEFAULT_MERGE_ANGLE_DEG = 25.0
DEFAULT_MIN_DISP_PX = 20.0  # 이보다 짧은 변위는 정지·주차로 보고 제외


def _heading_of(vec: Vector) -> tuple[float, float, float, float]:
    """변위 벡터 → (중점x, 중점y, 헤딩도, 변위길이). 산술이 반복돼 함수로 뺀다."""
    x0, y0, x1, y1 = vec
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    heading = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0
    disp = math.hypot(x1 - x0, y1 - y0)
    return mx, my, heading, disp


def grid_cells(displacements: Sequence[Vector], frame_wh: tuple[int, int], *,
               cell_px: float = DEFAULT_CELL_PX,
               min_samples: int = DEFAULT_MIN_SAMPLES,
               variance_threshold: float = DEFAULT_VARIANCE_THRESHOLD,
               min_disp_px: float = DEFAULT_MIN_DISP_PX) -> list[CellResult]:
    """변위 벡터를 격자 칸으로 묶어 칸마다 원형 평균·판정 상태를 낸다."""
    fw, fh = frame_wh
    if fw <= 0 or fh <= 0 or cell_px <= 0:
        return []

    by_cell: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for vec in displacements:
        mx, my, heading, disp = _heading_of(vec)
        if disp < min_disp_px:
            continue  # 정지·주차 — 방향 정보가 없다
        cx, cy = int(mx // cell_px), int(my // cell_px)
        by_cell.setdefault((cx, cy), []).append((mx, my, heading))

    out: list[CellResult] = []
    for (cx, cy), samples in by_cell.items():
        headings = [h for _mx, _my, h in samples]
        center = ((cx + 0.5) * cell_px, (cy + 0.5) * cell_px)
        n = len(samples)
        if n < min_samples:
            out.append(CellResult(cell=(cx, cy), center=center, n=n,
                                  headings_deg=headings, status="insufficient"))
            continue
        var = circular_variance(headings)
        if var > variance_threshold or is_bimodal(headings):
            out.append(CellResult(cell=(cx, cy), center=center, n=n,
                                  headings_deg=headings, variance=var,
                                  status="review"))
            continue
        out.append(CellResult(
            cell=(cx, cy), center=center, n=n, headings_deg=headings,
            mean_heading_deg=circular_mean_deg(headings), variance=var,
            status="propose"))
    return out


# --- 인접 칸 병합 + 화살표 생성 -------------------------------------------------


def _merge_groups(cells: list[CellResult],
                  merge_angle_deg: float) -> list[list[CellResult]]:
    """제안 가능("propose") 칸끼리, 4방향으로 붙어 있고 헤딩이 비슷하면
    하나의 무리로 묶는다(union-find). 화살표 개수를 줄이는 목적이다."""
    proposeable = {c.cell: c for c in cells if c.status == "propose"}
    parent: dict[tuple[int, int], tuple[int, int]] = {c: c for c in proposeable}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (cx, cy), cell in proposeable.items():
        for nb in ((cx + 1, cy), (cx, cy + 1)):  # 우·하 방향만 보면 왕복 충분(무향 그래프)
            if nb not in proposeable:
                continue
            diff = circular_angle_diff_deg(
                cell.mean_heading_deg, proposeable[nb].mean_heading_deg)
            if diff <= merge_angle_deg:
                union((cx, cy), nb)

    groups: dict[tuple[int, int], list[CellResult]] = {}
    for key, cell in proposeable.items():
        groups.setdefault(find(key), []).append(cell)
    return list(groups.values())


def propose_arrows(displacements: Sequence[Vector], frame_wh: tuple[int, int], *,
                   cell_px: float = DEFAULT_CELL_PX,
                   min_samples: int = DEFAULT_MIN_SAMPLES,
                   variance_threshold: float = DEFAULT_VARIANCE_THRESHOLD,
                   merge_angle_deg: float = DEFAULT_MERGE_ANGLE_DEG,
                   min_disp_px: float = DEFAULT_MIN_DISP_PX,
                   arrow_len_px: float | None = None) -> dict:
    """관측된 변위 벡터에서 통행 방향 화살표(안)을 낸다.

    반환값은 **저장 형식이 아니다** — ``review_needed`` 가 있으면 그 칸은
    사람이 직접 그려야 하고, ``arrows`` 도 관리자가 확인하기 전까지는
    "검토 대기"로 다뤄야 한다(``core/cameras.py`` 의 ``flow_arrows`` 에
    바로 쓰지 않는다).

    반환:
        {
          "arrows": [{"points": [[x1,y1],[x2,y2]], "n": 표본수,
                     "cells": [(cx,cy), ...]}, ...],
          "review_needed": [{"center": [x,y], "n": 표본수,
                            "reason": "양봉" | "분산 큼"}, ...],
          "insufficient": [{"center": [x,y], "n": 표본수}, ...],
        }
    """
    arrow_len = arrow_len_px if arrow_len_px is not None else cell_px * 1.2
    cells = grid_cells(displacements, frame_wh, cell_px=cell_px,
                       min_samples=min_samples,
                       variance_threshold=variance_threshold,
                       min_disp_px=min_disp_px)

    groups = _merge_groups(cells, merge_angle_deg)
    arrows = []
    for group in groups:
        all_headings = [h for c in group for h in c.headings_deg]
        mean_heading = circular_mean_deg(all_headings)
        # 무리 전체의 무게중심(표본 수로 가중) — 칸 하나짜리 무리면 그
        # 칸의 중심과 같다.
        total_n = sum(c.n for c in group)
        gx = sum(c.center[0] * c.n for c in group) / total_n
        gy = sum(c.center[1] * c.n for c in group) / total_n
        rad = math.radians(mean_heading)
        dx, dy = math.cos(rad) * arrow_len / 2.0, math.sin(rad) * arrow_len / 2.0
        arrows.append({
            "points": [[round(gx - dx, 1), round(gy - dy, 1)],
                      [round(gx + dx, 1), round(gy + dy, 1)]],
            "n": total_n,
            "cells": [c.cell for c in group],
        })

    review_needed = []
    insufficient = []
    for c in cells:
        if c.status == "review":
            reason = "양봉(왕복 차선이 한 칸에 겹침)" if is_bimodal(c.headings_deg) \
                else "방향이 흩어져 있음"
            review_needed.append({"center": [round(c.center[0], 1),
                                            round(c.center[1], 1)],
                                  "n": c.n, "reason": reason})
        elif c.status == "insufficient":
            insufficient.append({"center": [round(c.center[0], 1),
                                            round(c.center[1], 1)],
                                 "n": c.n})

    return {"arrows": arrows, "review_needed": review_needed,
           "insufficient": insufficient}
