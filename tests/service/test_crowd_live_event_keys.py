"""``/api/crowd/live`` 가 만드는 인파 이벤트의 block_id·detail 이 맞는가
(2026-08-22 전수점검).

**신고받은 문제.** 이 경로는 ``CrowdSnapshot.to_dict()``(키: ``person_count``)
결과를 그대로 ``event_sync.record_crowd_snapshot()``(기대 키:
``people_count``·``block_id``·``node_id``)에 넘겨, 이 경로로 만들어진 인파
이벤트는 **항상** ``block_id="CROWD"`` 로 뭉개지고 현재 인원 detail 이
**항상** 비어 있었다. 같은 함수를 올바르게 부르는 ``continuous.py``·
``crowd_service.py`` 의 CCTV 선택 탐지와 대조해 확인했다.

⚠️ 2026-08-31 — API 게이트웨이 Phase 1로 ``/api/crowd/live``가
``service/main.py``에서 ``service/crowd_service.py``(별도 서비스, 포트
8034)로 옮겨졌다. 이 시험도 그대로 따라간다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.service import crowd_service


class _FakeSnapshot:
    def __init__(self, **kv):
        self._kv = kv

    def to_dict(self):
        return dict(self._kv)


class _FakeAnalyzer:
    """분석기 흉내 — block_id/node_id 를 스스로 안다(실제
    ``CrowdLiveAnalyzer`` 와 동일)."""

    block_id = "SCOPE-CROWDLIVE-1"
    node_id = "가짜지점"

    def step(self, t_sec: float):
        return _FakeSnapshot(person_count=7, severity=1, events=[],
                            source="mock", risk_code="", risk_name="")


@pytest.fixture(autouse=True)
def _fake_analyzer(monkeypatch):
    monkeypatch.setattr(crowd_service, "_crowd_analyzer", _FakeAnalyzer())
    monkeypatch.setattr(crowd_service, "_crowd_t0", 0.0)
    # spread 계산은 이 시험과 무관하므로 무거운 실계산을 건너뛴다.
    monkeypatch.setattr(crowd_service, "_crowd_spread_cached", lambda: None)


def test_block_id와_현재인원이_분석기_값으로_채워진다(monkeypatch):
    captured = {}

    def _spy(snap):
        captured.update(snap)

    monkeypatch.setattr(crowd_service.event_sync, "record_crowd_snapshot", _spy)

    data = crowd_service.api_crowd_live()

    assert captured.get("block_id") == "SCOPE-CROWDLIVE-1"
    assert captured.get("node_id") == "가짜지점"
    assert captured.get("people_count") == 7
    # 응답 자체(to_dict 원본 키)는 그대로 person_count 를 쓴다 — 화면(app.js)
    # 이 이미 이 이름으로 읽고 있으므로 응답 계약은 바꾸지 않는다.
    assert data.get("person_count") == 7
