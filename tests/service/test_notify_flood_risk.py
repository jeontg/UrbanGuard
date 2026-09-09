"""``runner._notify_flood_risk`` — 침수 위험도의 독립 SOLAPI 알림 (확정사항
③, flood/traffic 도메인 분리, 2026-08-21).

## 왜 이 시험이 있나

예전에는 flood_risk_score/grade가 화면 표시용일 뿐, SOLAPI 알림은 교통·기상
판정(``_notify_decision``)에서만 나갔다. 이 시험은 (1) 등급 미만이면 알림이
안 나가는지, (2) 등급 이상이면 나가는지, (3) event_key가 교통 쪽
(``_notify_decision``, risk_code 기반)과 절대 겹치지 않는지를 확인한다.
"""
from __future__ import annotations

from types import SimpleNamespace

from tot_dashboard.service.runner import _notify_decision, _notify_flood_risk


def _risk(grade: int, score: float = 55.0) -> SimpleNamespace:
    return SimpleNamespace(risk_grade=grade, risk_score=score,
                           grade_label="높음", top_reason="도로 침수 면적")


def _flood_m(t: float = 12.0) -> SimpleNamespace:
    return SimpleNamespace(timestamp_sec=t)


class _FakeNotifier:
    def __init__(self):
        self.calls: list[dict] = []

    def send(self, *, event_key, message, channels):
        self.calls.append({"event_key": event_key, "message": message, "channels": channels})
        return {"dry_run": True}


def test_등급이_기준_미만이면_알림을_보내지_않는다():
    notifier = _FakeNotifier()
    _notify_flood_risk(notifier, _flood_m(), _risk(3), "시험지점", min_grade=4)
    assert notifier.calls == []


def test_등급이_기준_이상이면_알림을_보낸다():
    notifier = _FakeNotifier()
    _notify_flood_risk(notifier, _flood_m(), _risk(4), "시험지점", min_grade=4)
    assert len(notifier.calls) == 1
    assert "침수위험도" in notifier.calls[0]["message"]["body"]


def test_event_key에_등급이_들어가_등급별로_독립_취급된다():
    notifier = _FakeNotifier()
    _notify_flood_risk(notifier, _flood_m(), _risk(4), "시험지점", min_grade=4)
    _notify_flood_risk(notifier, _flood_m(), _risk(5), "시험지점", min_grade=4)
    keys = [c["event_key"] for c in notifier.calls]
    assert keys == ["시험지점:flood_risk:grade4", "시험지점:flood_risk:grade5"]
    assert keys[0] != keys[1]  # 등급이 오르면 쿨다운 없이 즉시 별개 이벤트로 취급됨


def test_침수_알림과_교통_알림의_event_key는_절대_겹치지_않는다():
    """★ 확정사항 ② — 혼합 상황에서 두 알림이 동시에 나갈 수 있어야 한다.
    두 함수가 만드는 event_key 네임스페이스가 겹치면 쿨다운 캐시가
    (event_key, channel) 키라서 한쪽이 다른 쪽을 조용히 억누르게 된다."""
    notifier = _FakeNotifier()
    traffic_decision = SimpleNamespace(
        level=SimpleNamespace(value="경계"), risk_name="강우 정체",
        recommendation="정체 구간 공유", context="테스트", t_sec=1.0,
        score=0.7, risk_code="TWR_RAIN_CONGESTION")
    _notify_decision(notifier, traffic_decision, "시험지점")
    _notify_flood_risk(notifier, _flood_m(), _risk(4), "시험지점", min_grade=4)
    keys = [c["event_key"] for c in notifier.calls]
    assert len(keys) == 2
    assert keys[0] != keys[1]
