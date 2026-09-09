"""노면 누적 탐지율이 화면에 드러나는가 (2026-08-19 전수조사).

## 왜 이 시험이 있나

전수조사에서 노면 관측 **664회 중 657회가 탐지 0건**(탐지율 1.05%)이었고,
부산 초량교차로는 **126회 전부 0건(0.0%)** 이었다.

화면에는 「부산 CCTV 에서 실사용 수준이 아니다」라는 **정성적 경고**만 있었다.
⚠️ **숫자가 없으면 두 가지가 안 된다.**

1. 관제요원이 「그래서 지금 얼마나 못 찾는가」를 모른다
2. 모델을 바꿔도 **나아졌는지 증명할 수단이 없다**

⚠️ **실패한 관측을 분모에 넣으면 안 된다** — 스트림이 끊겨 아무것도 못 본 것을
「못 찾았다」로 세면 모델이 실제보다 나빠 보인다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import road_history as RH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import RoadInspection

PFX = "TEST-RDS-"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(RoadInspection).where(
            RoadInspection.camera_id.like(f"{PFX}%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    RH.reset_cache()
    _purge()
    yield
    _purge()


def _add(camera_id: str, *, defects: int, failed: bool = False):
    db = get_session()
    try:
        db.add(RoadInspection(
            camera_id=camera_id, camera_name="시험지점", grade=0,
            defect_count=defects, frames_analyzed=0 if failed else 5,
            failed=failed, source="test", note="",
            analyzed_at=datetime.now(timezone.utc)))
        db.commit()
    finally:
        db.close()


def test_탐지율을_센다():
    for _ in range(9):
        _add(f"{PFX}A", defects=0)
    _add(f"{PFX}A", defects=3)
    s = RH.detection_stats(f"{PFX}A")
    assert s["analyzed"] == 10
    assert s["zero_detection"] == 9
    assert s["detection_rate"] == pytest.approx(0.1)


def test_실패한_관측은_분모에서_뺀다():
    """⚠️ 못 본 것을 「못 찾았다」로 세면 모델이 실제보다 나빠 보인다."""
    _add(f"{PFX}B", defects=1)
    _add(f"{PFX}B", defects=0, failed=True)
    s = RH.detection_stats(f"{PFX}B")
    assert s["total"] == 2
    assert s["failed"] == 1
    assert s["analyzed"] == 1
    # 실패를 0건으로 세지 않았다.
    assert s["zero_detection"] == 0
    assert s["detection_rate"] == pytest.approx(1.0)


def test_분석이_없으면_비율을_말하지_않는다():
    """★ 0/0 을 「0%」로 답하면 **아무것도 안 했는데 다 찾은 것처럼** 보인다."""
    _add(f"{PFX}C", defects=0, failed=True)
    s = RH.detection_stats(f"{PFX}C")
    assert s["analyzed"] == 0
    assert s["detection_rate"] is None


def test_화면이_탐지율을_말한다():
    """숫자와 경고문이 화면 코드에 있어야 한다."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
          / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "_roadStatsHtml" in js
    assert "탐지율" in js
    # ⚠️ 「탐지 0건 = 손상 없음」 오해를 막는 문구가 반드시 있어야 한다.
    assert "손상 없음" in js


# --- ② 구간 보정 진척 (2026-08-19 전수조사) --------------------------------
#
# 실측 결과 **39지점 전부 구간 길이가 비어 있었다**(0/39). 입력 화면(S-80)은
# 있는데 아무도 채우지 않았고, **화면 어디에도 그 사실이 드러나지 않았다.**
#
# ⚠️ 「미보정」만 보이면 고장으로 읽거나 그냥 넘긴다.


def test_구간_보정_진척을_화면이_말한다():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
          / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "_roadCalibHtml" in js
    assert "구간 보정" in js
    # ★ 어디서 채우는지 말하지 않으면 아무도 못 채운다.
    assert "S-80" in js
    # ⚠️ 「미보정 ≠ 양호」를 화면이 못박아야 한다.
    assert "「양호」가 아닙니다" in js


def test_section_length_는_보정_안_되면_None():
    from tot_dashboard.road import results as RR
    assert RR.section_length(f"{PFX}없는지점") is None
