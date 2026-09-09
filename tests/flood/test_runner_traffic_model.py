"""``PipelineRunner._build_source`` 가 실제로 지정된 교통 모델을 쓰는가
(2026-08-22 전수점검).

**신고받은 문제.** AI 모델 관리 화면(``/models``)에서 교통위험 검출 모델을
골라 저장해도, 실제 차량 검출은 그 값을 전혀 안 읽고 ``YoloDetectionSource``
의 하드코딩된 기본값(``models/yolo11s.pt``)만 계속 썼다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AppSetting
from tot_dashboard.service.runner import PipelineRunner

pytestmark = pytest.mark.usefixtures("db_schema")


@pytest.fixture(autouse=True)
def clean():
    _purge()
    yield
    _purge()


def _purge():
    s = get_session()
    try:
        keys = list(S.MODEL_KEYS.values())
        s.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
        s.commit()
    finally:
        s.close()
    S.invalidate()


def _make_runner() -> PipelineRunner:
    r = object.__new__(PipelineRunner)  # __init__ 의 무거운 모델 적재 없이 메서드만 시험
    r.fps = 5.0
    return r


def test_설정한_교통_모델을_실제로_넘긴다(monkeypatch):
    db = get_session()
    try:
        S.set_value(db, S.KEY_MODEL_TRAFFIC, "models/candidates/yolo26n.pt")
        db.commit()
    finally:
        db.close()
    S.invalidate()

    captured = {}

    class _FakeSource:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)

    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _FakeSource)

    runner = _make_runner()
    block = {"id": "TEST-TM-1", "source": {"type": "hls", "url": "https://x.test/a.m3u8"}}
    src, hint, kind = runner._build_source(block, rainfall=0.0)

    assert kind == "hls"
    assert captured.get("model") == "models/candidates/yolo26n.pt"


def test_설정이_없으면_라이브러리_기본값으로_물러난다(monkeypatch):
    """빈 설정에서 model_ops.selected_key 가 못 찾으면(레지스트리에도 기본값
    이 없으면) model= 을 아예 안 넘겨 YoloDetectionSource 자신의 기본값이
    쓰이게 한다 — 하위호환."""
    captured = {}

    class _FakeSource:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)

    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _FakeSource)
    monkeypatch.setattr(
        "tot_dashboard.core.model_ops.selected_key", lambda domain, db=None: "")

    runner = _make_runner()
    block = {"id": "TEST-TM-2", "source": {"type": "hls", "url": "https://x.test/a.m3u8"}}
    runner._build_source(block, rainfall=0.0)

    assert "model" not in captured
