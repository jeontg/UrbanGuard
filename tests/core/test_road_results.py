"""노면 탐지 결과 보관과 현황 표시 (road/results.py).

핵심은 **분석하지 않은 지점을 「정상」으로 보여 주지 않는가**다.
모의값으로 채우면 점검하지 않은 구간을 안전한 것으로 오해한다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.road import results as R


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """이 파일은 **메모리 저장소의 동작**을 검증한다.

    ``history()`` 는 DB가 있으면 그쪽을 먼저 읽으므로, 기본적으로 DB 경로를
    막아 메모리 동작만 보이게 한다. DB 연동은 아래 전용 테스트와
    `tests/core/test_road_history.py` 가 따로 검증한다.
    """
    from tot_dashboard.core import road_history
    monkeypatch.setattr(road_history, "recent", lambda cid, n: None)
    monkeypatch.setattr(road_history, "save", lambda *a, **k: False)
    R.clear()
    yield
    R.clear()


def test_분석_전에는_등급을_매기지_않는다():
    s = R.summary("CAM-A", "가지점")
    assert s["analyzed"] is False
    assert s["grade"] is None
    assert s["grade_label"] == "미분석"
    assert s["defect_count"] == 0


def test_분석하면_결과가_현황에_반영된다():
    R.record("CAM-A", {"grade": 3, "grade_label": "주의",
                       "defects": [{"type": "pothole"}, {"type": "crack"}],
                       "frames_analyzed": 12})
    s = R.summary("CAM-A", "가지점")
    assert s["analyzed"] is True
    assert s["grade"] == 3 and s["grade_label"] == "주의"
    assert s["defect_count"] == 2
    assert s["frames_analyzed"] == 12
    assert s["analyzed_at"]


def test_같은_지점을_다시_분석하면_최신으로_덮는다():
    R.record("CAM-A", {"grade": 4, "defects": [{"type": "pothole"}],
                       "frames_analyzed": 6})
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 6})
    s = R.summary("CAM-A", "가지점")
    assert s["grade"] == 1 and s["defect_count"] == 0


def test_지점끼리_섞이지_않는다():
    R.record("CAM-A", {"grade": 4, "defects": [{"type": "pothole"}],
                       "frames_analyzed": 6})
    assert R.summary("CAM-B", "나지점")["analyzed"] is False


def test_탐지_0건도_분석한_것으로_남긴다():
    """0건은 「미분석」과 다르다 — 봤는데 없었다는 뜻이다."""
    R.record("CAM-A", {"grade": 1, "grade_label": "양호",
                       "defects": [], "frames_analyzed": 8})
    s = R.summary("CAM-A", "가지점")
    assert s["analyzed"] is True
    assert s["defect_count"] == 0
    assert s["grade_label"] == "양호"


def test_스트림_오류는_사유를_남긴다():
    R.record("CAM-A", {"defects": [], "frames_analyzed": 0,
                       "note": "스트림에서 프레임을 받지 못했습니다."})
    s = R.summary("CAM-A", "가지점")
    assert "프레임" in s["note"]
    assert s["failed"] is True


def test_빈_카메라ID나_잘못된_값은_무시한다():
    R.record("", {"grade": 1})
    R.record("CAM-A", None)
    assert R.all_results() == {}


# --- 프레임 0건 처리 (2026-08-11 발견) --------------------------------------

def test_프레임을_못_받으면_정상이_아니라_분석_실패다():
    """스트림이 끊겨 한 장도 못 본 구간이 「점검했더니 이상 없음」으로
    보이면 안 된다. 실제로 동영상 지점이 그렇게 표시됐다."""
    R.record("CAM-A", {"grade": 1, "grade_label": "정상", "defects": [],
                       "frames_analyzed": 0,
                       "note": "이 블록은 실시간 스트림이 없습니다."})
    s = R.summary("CAM-A", "가지점")
    assert s["analyzed"] is False
    assert s["failed"] is True
    assert s["grade_label"] == "분석 실패"
    assert s["grade"] is None
    assert "스트림" in s["note"]


def test_분석_실패와_미분석은_다르게_표시한다():
    """둘 다 결과가 없지만, 시도했는지 여부가 다르다."""
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 0})
    fail = R.summary("CAM-A", "가지점")
    never = R.summary("CAM-B", "나지점")
    assert (fail["failed"], fail["grade_label"]) == (True, "분석 실패")
    assert (never["failed"], never["grade_label"]) == (False, "미분석")


def test_프레임이_한_장이라도_있으면_분석으로_본다():
    R.record("CAM-A", {"grade": 1, "grade_label": "정상", "defects": [],
                       "frames_analyzed": 1})
    s = R.summary("CAM-A", "가지점")
    assert s["analyzed"] is True and s["failed"] is False


# --- 신선도·이력 (S-44 실시간 관제, 2026-08-12) ------------------------------

def test_경과_시간을_함께_내려준다():
    """상시 순회는 15분 주기라, 등급만으로는 방금 것인지 알 수 없다."""
    R.record("CAM-A", {"grade": 1, "grade_label": "정상", "defects": [],
                       "frames_analyzed": 5})
    s = R.summary("CAM-A", "가지점")
    assert s["age_sec"] is not None
    assert s["age_sec"] < 5          # 방금 기록했으므로


def test_관측_이력이_없으면_경과_시간도_없다():
    """0초로 채우면 「방금 관측함」으로 보인다 — 그 반대다."""
    s = R.summary("CAM-A", "가지점")
    assert s["age_sec"] is None


def test_시각을_모르면_경과_시간을_추측하지_않는다():
    assert R.age_seconds(None) is None
    assert R.age_seconds("어제쯤") is None


def test_관측_이력이_쌓인다():
    for n in range(3):
        R.record("CAM-A", {"grade": 2, "defects": [{"type": "pothole"}] * n,
                           "frames_analyzed": 4})
    h = R.history("CAM-A")
    assert [x["defect_count"] for x in h] == [0, 1, 2]
    assert all(x["failed"] is False for x in h)


def test_이력에도_분석_실패가_구분되어_남는다():
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 0})
    assert R.history("CAM-A")[-1]["failed"] is True


def test_이력은_상한을_넘겨_쌓이지_않는다():
    """메모리 저장이라 무한정 쌓으면 장기 운영에서 새어 나간다."""
    for _ in range(R.HISTORY_LIMIT + 15):
        R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 1})
    assert len(R.history("CAM-A", limit=999)) == R.HISTORY_LIMIT


def test_clear는_이력까지_지운다():
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 1})
    R.clear()
    assert R.history("CAM-A") == []

# --- 이력 영구 보관 폴백 (2026-08-14) ----------------------------------------

def test_출처를_이력에_남긴다():
    """상시 순회와 사람이 눌러 돌린 결과를 구분해야 값이 튀는 이유를 안다."""
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 3},
             source="manual")
    assert R.history("CAM-A")[-1]["source"] == "manual"


def test_DB를_못_쓰면_메모리_이력으로_내려간다(monkeypatch):
    """DB가 죽어도 화면은 그대로 동작해야 한다."""
    from tot_dashboard.core import road_history
    monkeypatch.setattr(road_history, "recent", lambda cid, n: None)
    monkeypatch.setattr(road_history, "save", lambda *a, **k: False)
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 3})
    assert len(R.history("CAM-A")) == 1


def test_DB에_있으면_그쪽을_먼저_읽는다(monkeypatch):
    """재시작 후에도 이력이 이어져야 한다 — 메모리는 비어 있어도 나와야 한다."""
    from tot_dashboard.core import road_history
    fake = [{"analyzed_at": "2026-08-13T00:00:00+00:00", "defect_count": 9,
             "grade": 4, "frames_analyzed": 7, "failed": False,
             "source": "continuous"}]
    monkeypatch.setattr(road_history, "recent", lambda cid, n: fake)
    R.clear()                       # 메모리는 비운다
    assert R.history("CAM-A") == fake


def test_DB_저장이_터져도_기록은_계속된다(monkeypatch):
    """관제 분석이 이력 저장 때문에 멈추면 안 된다."""
    from tot_dashboard.core import road_history

    def _boom(*a, **k):
        raise RuntimeError("DB 오류")
    monkeypatch.setattr(road_history, "save", _boom)
    monkeypatch.setattr(road_history, "recent", lambda cid, n: None)
    R.record("CAM-A", {"grade": 1, "defects": [], "frames_analyzed": 3})
    assert R.summary("CAM-A", "가지점")["analyzed"] is True
