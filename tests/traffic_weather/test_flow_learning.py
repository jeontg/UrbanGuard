"""통행 방향 화살표 자동 생성 (``flow_learning``) — Phase 4-A, 2026-08-26.

지켜야 할 것.

* **원형 평균은 산술평균과 다르다** — 350°와 10°의 평균은 180°가 아니라
  0° 근처여야 한다
* **양봉(왕복 차선이 한 칸에 겹침)은 억지로 평균 내지 않는다** — "검토 대상"
  으로만 표시하고 화살표를 만들지 않는다
* **표본이 적으면 아무 판정도 하지 않는다**
* **정지·주차 차량의 짧은 변위는 방향 정보로 안 쓴다**
* **결과는 저장 형식이 아니다** — 전부 "제안"이며 확정은 사람이 한다
"""
from __future__ import annotations

import math

import pytest

from tot_dashboard.traffic_weather.perception import flow_learning as FL


# --- 원형 통계 -----------------------------------------------------------------


def test_350도와_10도의_평균은_0도_근처다():
    """산술평균(180°)이 아니라 원형 평균(0° 근처)이어야 한다."""
    mean = FL.circular_mean_deg([350.0, 10.0])
    assert mean == pytest.approx(0.0, abs=1.0) or mean == pytest.approx(360.0, abs=1.0)


def test_한_방향으로_모이면_그_방향이_평균이다():
    mean = FL.circular_mean_deg([88.0, 90.0, 92.0])
    assert mean == pytest.approx(90.0, abs=1.0)


def test_빈_목록의_평균은_0도():
    assert FL.circular_mean_deg([]) == 0.0


def test_한_방향으로_모이면_분산이_0에_가깝다():
    var = FL.circular_variance([90.0, 91.0, 89.0, 90.0])
    assert var < 0.01


def test_사방으로_흩어지면_분산이_1에_가깝다():
    var = FL.circular_variance([0.0, 90.0, 180.0, 270.0])
    assert var > 0.9


def test_빈_목록의_분산은_1():
    assert FL.circular_variance([]) == 1.0


def test_350도와_10도_차이는_340이_아니라_20도():
    assert FL.circular_angle_diff_deg(350.0, 10.0) == pytest.approx(20.0)


def test_0도와_180도_차이는_180도():
    assert FL.circular_angle_diff_deg(0.0, 180.0) == pytest.approx(180.0)


def test_한_방향으로_모이면_양봉이_아니다():
    assert FL.is_bimodal([88.0, 90.0, 92.0, 89.0, 91.0]) is False


def test_반반_갈리면_양봉이다():
    """왕복 차선이 한 칸에 겹친 상황."""
    assert FL.is_bimodal([0.0, 2.0, -2.0, 178.0, 180.0, 182.0]) is True


def test_소수_잡음은_양봉으로_보지_않는다():
    """9대는 0도, 1대만 반대(180도) — 단봉으로 봐야 억지 평균을 안 낸다."""
    headings = [0.0] * 9 + [180.0]
    assert FL.is_bimodal(headings) is False


def test_정확히_반반이면_양봉이다():
    """★ 회귀 방지 — 정확히 5:5로 갈리면 원형 평균 자체가 상쇄돼(0에
    가까움) 그걸 축으로 쓰면 부동소수 오차로 전부 한쪽으로 잘못
    분류된다. 두 배로 접는 축 찾기로 이 경계를 제대로 잡아야 한다."""
    headings = [0.0] * 5 + [180.0] * 5
    assert FL.is_bimodal(headings) is True


# --- 격자 집계 -----------------------------------------------------------------

FRAME_WH = (900, 900)


def _vec(mx, my, heading_deg, length=60.0):
    """중점(mx,my)·방향·길이로 변위 벡터를 만든다(테스트 편의용 역산)."""
    rad = math.radians(heading_deg)
    dx, dy = math.cos(rad) * length / 2.0, math.sin(rad) * length / 2.0
    return (mx - dx, my - dy, mx + dx, my + dy)


def test_표본이_적으면_insufficient():
    vecs = [_vec(75, 75, 0.0) for _ in range(2)]  # min_samples(5) 미만
    cells = FL.grid_cells(vecs, FRAME_WH, cell_px=150, min_samples=5)
    assert len(cells) == 1
    assert cells[0].status == "insufficient"


def test_단봉이면_propose():
    vecs = [_vec(75, 75, 90.0 + i) for i in range(-2, 3)]  # 88~92도, 5대
    cells = FL.grid_cells(vecs, FRAME_WH, cell_px=150, min_samples=5)
    assert len(cells) == 1
    assert cells[0].status == "propose"
    assert cells[0].mean_heading_deg == pytest.approx(90.0, abs=2.0)


def test_양봉이면_review():
    vecs = ([_vec(75, 75, 0.0) for _ in range(5)]
           + [_vec(75, 75, 180.0) for _ in range(5)])
    cells = FL.grid_cells(vecs, FRAME_WH, cell_px=150, min_samples=5)
    assert len(cells) == 1
    assert cells[0].status == "review"


def test_정지차량_변위는_제외된다():
    """변위가 min_disp_px 미만이면 방향 정보로 안 쓴다 — 표본 자체가 안 생긴다."""
    vecs = [_vec(75, 75, 0.0, length=5.0) for _ in range(10)]  # 문턱(20px) 미만
    cells = FL.grid_cells(vecs, FRAME_WH, cell_px=150, min_samples=5,
                          min_disp_px=20.0)
    assert cells == []


def test_격자_크기나_해상도가_0이면_빈_목록():
    assert FL.grid_cells([_vec(75, 75, 0.0)], (0, 0)) == []
    assert FL.grid_cells([_vec(75, 75, 0.0)], FRAME_WH, cell_px=0) == []


# --- 병합 + 화살표 생성 --------------------------------------------------------


def test_인접한_비슷한_방향_칸은_하나로_병합된다():
    """두 칸이 붙어 있고 방향이 비슷하면 화살표 하나로 합친다 — 39대마다
    칸 수만큼 화살표가 나오면 편집이 오히려 번거로워진다."""
    vecs = ([_vec(75, 75, 90.0 + i) for i in range(-2, 3)]     # 칸(0,0)
           + [_vec(75, 225, 90.0 + i) for i in range(-2, 3)])  # 칸(0,1), 붙어있음
    res = FL.propose_arrows(vecs, FRAME_WH, cell_px=150, min_samples=5)
    assert len(res["arrows"]) == 1
    assert res["arrows"][0]["n"] == 10


def test_방향이_다른_인접칸은_병합되지_않는다():
    vecs = ([_vec(75, 75, 0.0) for _ in range(5)]        # 칸(0,0): 오른쪽
           + [_vec(75, 225, 180.0) for _ in range(5)])   # 칸(0,1): 왼쪽(반대)
    res = FL.propose_arrows(vecs, FRAME_WH, cell_px=150, min_samples=5,
                            merge_angle_deg=25.0)
    assert len(res["arrows"]) == 2


def test_양봉_칸은_화살표_대신_검토_대상으로_나온다():
    vecs = ([_vec(75, 75, 0.0) for _ in range(5)]
           + [_vec(75, 75, 180.0) for _ in range(5)])
    res = FL.propose_arrows(vecs, FRAME_WH, cell_px=150, min_samples=5)
    assert res["arrows"] == []
    assert len(res["review_needed"]) == 1
    assert "양봉" in res["review_needed"][0]["reason"]


def test_화살표_방향이_실제_관측_방향과_일치한다():
    """오른쪽(+x)으로 관측됐으면 화살표도 오른쪽을 가리켜야 한다 —
    반대로 나오면 역주행 판정이 통째로 뒤집힌다."""
    vecs = [_vec(75, 75, 0.0) for _ in range(6)]
    res = FL.propose_arrows(vecs, FRAME_WH, cell_px=150, min_samples=5)
    assert len(res["arrows"]) == 1
    (x1, _y1), (x2, _y2) = res["arrows"][0]["points"]
    assert x2 > x1, "오른쪽 관측인데 화살표가 왼쪽을 가리킨다"


def test_결과에_저장_형식이_아니라는_구조가_그대로_드러난다():
    """review_needed/insufficient 가 arrows 와 분리돼 있어야, 호출부가
    "확정"과 "검토 필요"를 헷갈리지 않는다."""
    res = FL.propose_arrows([], FRAME_WH)
    assert set(res.keys()) == {"arrows", "review_needed", "insufficient"}
    assert res["arrows"] == []
