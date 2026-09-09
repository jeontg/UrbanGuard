"""운영 모델 선택 (core/model_ops.py) 과 상태 도출 (core/analytics.py).

지켜야 할 것.

* **반영 시점을 감추지 않는다** — 도메인마다 모델을 붙드는 방식이 달라 즉시
  반영되는 것과 재기동해야 하는 것이 있다. 「저장했습니다」로만 끝내면 바뀐
  줄 알고 관제하게 된다
* **재기동해도 선택이 남는다** — 설정은 남아 있는데 코드 기본값이 도는 상태가
  가장 헷갈린다
* **상태를 손으로 적지 않는다** — 예전에는 「v0.3 · 개발중」이 코드에 박혀 있어
  모델을 바꿔도 화면이 그대로였다
* **탐지 0건을 「운영중」으로 부르지 않는다** — 아무것도 못 잡는 상태와 평온한
  상태가 같아 보이면 안 된다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from tot_dashboard.core import analytics as A
from tot_dashboard.core import model_ops
from tot_dashboard.core import model_registry as MR
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AppSetting

pytestmark = pytest.mark.usefixtures("db_schema")


@pytest.fixture()
def db():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def clean():
    _purge()
    yield
    _purge()


def _purge():
    s = get_session()
    try:
        keys = list(S.MODEL_KEYS.values()) + list(S.MODEL_NOTE_KEYS.values())
        s.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
        s.commit()
    finally:
        s.close()
    S.invalidate()


def _road_models():
    rows = MR.for_domain("road")
    if len(rows) < 2:
        pytest.skip("이 환경에는 비교할 노면 모델이 둘 이상 없다")
    return rows


# --- 선택 -------------------------------------------------------------------
def test_지정하지_않으면_코드_기본값을_쓴다(db):
    default = MR.default_for("road")
    if default is None:
        pytest.skip("이 환경에 노면 모델이 없다")
    assert model_ops.selected_key("road", db) == default.key


def test_지정하면_그것이_운영_모델이_된다(db):
    rows = _road_models()
    other = rows[1]
    model_ops.choose(db, "road", other.key)
    S.invalidate()
    assert model_ops.selected_key("road", db) == other.key


def test_노면은_즉시_반영된다(db, monkeypatch):
    """상시 순회가 분석기 하나를 계속 쓰므로, 내리지 않고 갈아 끼워야 한다."""
    rows = _road_models()
    swapped = {}

    class FakeAnalyzer:
        def set_model(self, path):
            swapped["path"] = path
            return True

    import tot_dashboard.service.main as service_main
    monkeypatch.setattr(service_main, "_road_analyzer", FakeAnalyzer(),
                        raising=False)

    res = model_ops.choose(db, "road", rows[1].key)
    assert res["applied"] is True
    assert swapped["path"] == rows[1].key


def test_침수_인파는_재기동이_필요하다고_알린다(db):
    """감추면 바뀐 줄 알고 관제하게 된다."""
    rows = MR.for_domain("flood")
    if not rows:
        pytest.skip("이 환경에 침수 모델이 없다")
    res = model_ops.choose(db, "flood", rows[0].key)
    assert res["applied"] is False
    assert "재기동" in res["note"]


def test_반영에_실패하면_성공했다고_말하지_않는다(db, monkeypatch):
    rows = _road_models()

    class Broken:
        def set_model(self, path):
            raise RuntimeError("교체 실패")

    import tot_dashboard.service.main as service_main
    monkeypatch.setattr(service_main, "_road_analyzer", Broken(), raising=False)

    res = model_ops.choose(db, "road", rows[1].key)
    assert res["applied"] is False
    # 저장 자체는 남아야 한다 — 재기동하면 적용된다.
    S.invalidate()
    assert model_ops.selected_key("road", db) == rows[1].key


def test_없는_모델은_거부한다(db):
    with pytest.raises(LookupError):
        model_ops.choose(db, "road", "data/nope/nope.pt")


def test_알_수_없는_도메인은_거부한다(db):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델이 없다")
    with pytest.raises(ValueError):
        model_ops.choose(db, "weather", rows[0].key)


def test_다른_도메인_전용_모델은_거부한다(db):
    """2026-08-22 전수점검 — 화면 드롭다운은 도메인별로 걸러서 보여줄 뿐
    방어선이 아니었다. 폼을 조작하면 노면 모델을 domain="flood" 로 저장할
    수 있었다."""
    road_only = [m for m in MR.for_domain("road") if m.domain == "road"]
    if not road_only:
        pytest.skip("이 환경에 도메인이 명확한 노면 모델이 없다")
    with pytest.raises(ValueError):
        model_ops.choose(db, "flood", road_only[0].key)


def test_기동_시_저장된_선택을_다시_적용한다(db, monkeypatch):
    """이게 없으면 재기동 후 코드 기본값으로 되돌아간다."""
    rows = _road_models()
    applied = {}

    class FakeAnalyzer:
        def set_model(self, path):
            applied["path"] = path
            return True

    import tot_dashboard.service.main as service_main
    monkeypatch.setattr(service_main, "_road_analyzer", FakeAnalyzer(),
                        raising=False)

    S.set_value(db, S.KEY_MODEL_ROAD, rows[1].key)
    db.commit()
    applied.clear()

    model_ops.prime_from_settings()
    assert applied.get("path") == rows[1].key


def test_지정한_모델_파일이_사라지면_기동을_막지_않는다(db, monkeypatch):
    S.set_value(db, S.KEY_MODEL_ROAD, "data/사라진/모델.pt")
    db.commit()
    # 예외가 새면 서비스 기동 자체가 실패한다.
    model_ops.prime_from_settings()


# --- 비고 -------------------------------------------------------------------
def test_비고는_사람이_적고_저장된다(db):
    model_ops.set_note(db, "road", "부산 CCTV 실사용 불가 — 참고 자료로만")
    S.invalidate()
    assert "참고 자료" in model_ops.note("road", db)


def test_비고_기본값이_있다(db):
    assert model_ops.note("road", db), "초기 비고가 비어 있다"


# --- 상태 도출 ---------------------------------------------------------------
def test_파일이_없으면_모델_없음(db):
    state, badge = A._model_state(None, 0)
    assert state == A.ST_MISSING and badge == "crit"


def test_탐지가_0건이면_운영중이라_부르지_않는다():
    """아무것도 못 잡는 상태와 평온한 상태가 같아 보이면 안 된다."""
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델이 없다")
    state, badge = A._model_state(rows[0], 0)
    assert state == A.ST_UNTESTED and badge == "warn"


def test_탐지_실적이_있으면_운영중():
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델이 없다")
    state, badge = A._model_state(rows[0], 5)
    assert state == A.ST_RUNNING and badge == "on"


def test_모델_현황은_실제_파일을_보여_준다(db):
    """예전에는 「v0.3」이 코드에 박혀 있어 모델을 바꿔도 그대로였다."""
    rows = _road_models()
    model_ops.choose(db, "road", rows[1].key)
    S.invalidate()
    stats = {r["domain"]: r for r in A.model_stats(db)}
    assert stats["road"]["model_key"] == rows[1].key
    assert rows[1].label in stats["road"]["version"]


def test_모델_현황의_비고는_설정에서_온다(db):
    model_ops.set_note(db, "crowd", "우리가 적은 판단")
    S.invalidate()
    stats = {r["domain"]: r for r in A.model_stats(db)}
    assert stats["crowd"]["note"] == "우리가 적은 판단"
