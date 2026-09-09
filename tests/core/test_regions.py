"""행정구역 — 시/도와 시·군·구.

지켜야 할 것.

* **시/도 없이 구·군만** 있는 것은 막는다 — 어느 시의 「중구」인지 알 수 없다
* **그 시/도에 없는 구·군**은 막는다 — 틀린 값으로 낸 통계는 전부 어긋난다
* **둘 다 비어도 등록은 된다** — 지역을 모르는 지점도 받아야 한다
* **구·군은 좌표로 유추하지 않는다** — 사각형으로는 구를 가를 수 없다
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import cctv_sources as SRC
from tot_dashboard.core import regions as RG


# ---------- 목록 자체 ----------

def test_시도_키가_좌표_프리셋과_같다():
    """두 벌로 두면 언젠가 어긋난다."""
    assert set(RG.SIGUNGU) == set(SRC.REGIONS)


def test_시군구가_비어_있는_시도는_없다():
    for key, names in RG.SIGUNGU.items():
        assert names, key


def test_같은_시도_안에_중복_이름이_없다():
    for key, names in RG.SIGUNGU.items():
        assert len(names) == len(set(names)), key


@pytest.mark.parametrize("sido,count", [
    ("seoul", 25), ("busan", 16), ("ulsan", 5), ("gyeongnam", 18),
    ("jeju", 2), ("gwangju", 5), ("daejeon", 5),
])
def test_주요_시도의_개수(sido, count):
    assert len(RG.SIGUNGU[sido]) == count


def test_군위군은_대구에_있고_경북에_없다():
    """2023년 7월 편입. 옛 자료를 그대로 쓰면 여기서 걸린다."""
    assert "군위군" in RG.SIGUNGU["daegu"]
    assert "군위군" not in RG.SIGUNGU["gyeongbuk"]


def test_세종은_자기_자신만_둔다():
    """하위 자치구가 없다."""
    assert RG.SIGUNGU["sejong"] == ("세종특별자치시",)


# ---------- 선택지 ----------

def test_시도_선택지는_17개():
    assert len(RG.sido_choices()) == 17


def test_모르는_시도의_구군_목록은_빈_목록():
    assert RG.sigungu_choices("없는키") == []
    assert RG.sigungu_choices("") == []
    assert RG.sigungu_choices(None) == []


# ---------- 검증 ----------

def test_정상_조합():
    assert RG.validate("seoul", "강남구") == []
    assert RG.validate("busan", "기장군") == []


def test_둘_다_비면_통과한다():
    """지역을 모르는 지점도 등록은 되어야 한다."""
    assert RG.validate("", "") == []
    assert RG.validate(None, None) == []


def test_시도만_있어도_통과한다():
    assert RG.validate("busan", "") == []


def test_시도_없이_구군만_있으면_막는다():
    errs = RG.validate("", "중구")
    assert errs and "시/도를 먼저" in errs[0]


def test_그_시도에_없는_구군은_막는다():
    errs = RG.validate("seoul", "수영구")     # 수영구는 부산
    assert errs and "없는 시·군·구" in errs[0]


def test_모르는_시도는_막는다():
    errs = RG.validate("도쿄", "")
    assert errs and "알 수 없는 시/도" in errs[0]


@pytest.mark.parametrize("sido,gu,ok", [
    ("seoul", "강남구", True),
    ("seoul", "", True),
    ("", "", True),
    ("", "강남구", False),
    ("seoul", "기장군", False),
    ("없는키", "", False),
])
def test_is_valid(sido, gu, ok):
    assert RG.is_valid(sido, gu) is ok


# ---------- 좌표 폴백 ----------

def test_좌표로_시도만_유추한다():
    assert RG.fallback_sido(37.5665, 126.9780) == "seoul"
    assert RG.fallback_sido(35.1796, 129.0756) == "busan"


def test_국외_좌표는_빈_문자열():
    assert RG.fallback_sido(35.68, 139.69) == ""
    assert RG.fallback_sido(None, None) == ""


# ---------- 표기 ----------

@pytest.mark.parametrize("sido,gu,want", [
    ("seoul", "강남구", "서울특별시 강남구"),
    ("busan", "", "부산광역시"),
    ("", "", "지역 미상"),
    (None, None, "지역 미상"),
])
def test_화면_표기(sido, gu, want):
    assert RG.label_of(sido, gu) == want
