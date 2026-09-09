"""오류 관리 코어 (core/errors.py, core/error_catalog.py).

지켜야 할 것.

* **오류 기록이 본래 업무를 막지 않는다** — 기록이 실패해도 예외가 밖으로
  새지 않는다. DB가 죽어서 오류를 못 남기는 상황에 관제까지 죽으면 안 된다
* **같은 오류는 묶는다** — CCTV 재접속 실패는 초당 수십 번 난다. 발생마다 행을
  만들면 정작 봐야 할 오류 한 건이 묻힌다
* **기록하다 다시 오류를 내지 않는다** — 재진입하면 무한히 돈다
* **처리완료 건에는 새 발생을 얹지 않는다** — 조치했는데 또 났다는 사실이
  가려지면 안 된다
* **비밀번호·API 키는 화면에 남기지 않는다** — 예외 메시지에 접속 문자열이
  통째로 실려 오는 일이 흔하다
* **삭제는 실제 삭제** — 사용자 지정. 다만 조건 없는 전체 삭제는 거부한다
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import error_catalog as catalog
from tot_dashboard.core import errors as E
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import ErrorCode, ErrorLog

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
        s.execute(sa_delete(ErrorLog))
        s.execute(sa_delete(ErrorCode))
        s.commit()
    finally:
        s.close()


# --- 기록과 집계 -------------------------------------------------------------
def test_기록하면_이력에_남는다(db):
    rid = E.record(message="스트림을 열 수 없습니다", source="worker",
                   path="road_live:88")
    assert rid is not None
    row = db.get(ErrorLog, rid)
    assert row.count == 1
    assert row.resolved is False
    assert row.source == "worker"


def test_같은_오류는_한_행으로_묶이고_횟수만_오른다(db):
    ids = [E.record(message="스트림을 열 수 없습니다", path="road_live:88")
           for _ in range(5)]
    assert len(set(ids)) == 1, "같은 오류가 여러 행으로 갈라졌다"
    db.expire_all()
    assert db.get(ErrorLog, ids[0]).count == 5


def test_숫자만_다른_메시지도_같은_오류로_묶인다(db):
    """카메라 번호·시각만 다른 메시지가 별개 오류로 잡히면 목록이 다시 폭주한다."""
    a = E.record(message="카메라 3 접속 실패 (192.168.0.11:554)", path="/x")
    b = E.record(message="카메라 17 접속 실패 (192.168.0.29:554)", path="/x")
    assert a == b
    db.expire_all()
    assert db.get(ErrorLog, a).count == 2


def test_다른_오류는_따로_남는다():
    a = E.record(message="스트림을 열 수 없습니다", path="/a")
    b = E.record(message="모델 파일을 찾을 수 없습니다", path="/b")
    assert a != b


def test_처리완료_건에는_새_발생을_얹지_않는다(db):
    """조치한 뒤 또 났다는 사실이 가려지면 안 된다."""
    first = E.record(message="스트림을 열 수 없습니다", path="/x")
    E.resolve(db, first, by="admin", note="회선 교체")
    second = E.record(message="스트림을 열 수 없습니다", path="/x")
    assert second != first, "처리완료 행에 새 발생이 합쳐졌다"
    db.expire_all()
    assert db.get(ErrorLog, first).count == 1


def test_집계_창을_벗어난_옛_오류는_새_행이_된다(db):
    """지난주 장애와 오늘 장애가 한 줄로 합쳐지면 「언제부터」가 사라진다."""
    old = E.record(message="스트림을 열 수 없습니다", path="/x")
    row = db.get(ErrorLog, old)
    row.last_seen_at = row.last_seen_at - E.AGGREGATE_WINDOW - timedelta(hours=1)
    db.commit()
    new = E.record(message="스트림을 열 수 없습니다", path="/x")
    assert new != old


# --- 죽지 않기 ---------------------------------------------------------------
def test_기록_실패는_예외를_밖으로_내지_않는다(monkeypatch):
    def boom():
        raise RuntimeError("DB 없음")
    monkeypatch.setattr(E, "get_session", boom)
    # 예외가 새면 이 줄에서 테스트가 깨진다.
    assert E.record(message="아무거나") is None


def test_재진입하면_기록하지_않는다(monkeypatch):
    """기록 경로에서 난 오류를 다시 기록하면 무한히 돈다."""
    seen = []

    def fake(**kw):
        seen.append(kw)
        # 기록 도중 또 기록을 시도하는 상황을 흉내 낸다.
        assert E.record(message="안쪽") is None
        return 1

    monkeypatch.setattr(E, "_record", fake)
    E.record(message="바깥쪽")
    assert len(seen) == 1


# --- 민감정보 ---------------------------------------------------------------
def test_비밀번호와_키는_가려진다(db):
    rid = E.record(
        message="could not connect: "
                "postgresql+psycopg://urbanguard:ug_dev_2026@127.0.0.1:5433/x",
        detail="api_key=AKIAVERYSECRET token: abcdef123456")
    row = db.get(ErrorLog, rid)
    assert "ug_dev_2026" not in row.message
    assert "urbanguard" in row.message, "사용자명까지 지우면 원인을 못 찾는다"
    assert "AKIAVERYSECRET" not in row.detail
    assert "abcdef123456" not in row.detail


# --- 분류 -------------------------------------------------------------------
@pytest.mark.parametrize("message, expected", [
    ("could not connect to server", "UG-DB-001"),
    ('relation "events" does not exist', "UG-DB-002"),
    ("getaddrinfo failed", "UG-CCTV-002"),
    ("VideoCapture 열기 실패", "UG-CCTV-001"),
    ("models/best.pt 없음", "UG-AI-001"),
    ("CUDA not available", "UG-AI-004"),
    ("No space left on device", "UG-FILE-003"),
    ("전혀 모르는 문구", catalog.UNKNOWN),
])
def test_메시지로_코드를_고른다(message, expected):
    assert catalog.classify(message=message) == expected


def test_상태코드가_예외보다_우선한다():
    assert catalog.classify(message="아무거나", status_code=403) == "UG-AUTH-003"


def test_분류에_실패해도_기록은_버리지_않는다(db):
    """처음 보는 오류일수록 남겨야 한다."""
    rid = E.record(message="처음 보는 알 수 없는 문제")
    assert db.get(ErrorLog, rid).code == catalog.UNKNOWN


def test_예외_클래스_이름으로도_고른다():
    class OperationalError(Exception):
        pass
    assert catalog.classify(OperationalError("x")) == "UG-DB-001"


# --- 검색 -------------------------------------------------------------------
def _seed():
    E.record(code="UG-CCTV-001", message="스트림 열기 실패", source="worker",
             path="/road")
    E.record(code="UG-DB-001", message="접속 실패", source="web", path="/api/x")
    E.record(code="UG-AI-001", message="모델 없음", source="pipeline", path="/y")


def test_코드로_검색한다(db):
    _seed()
    rows = E.search(db, code="UG-DB-001")
    assert [r.code for r in rows] == ["UG-DB-001"]


def test_분류로_검색한다(db):
    """코드 문자열 안의 분류로 좁힌다 — 사전이 비어 있어도 검색은 돼야 한다."""
    _seed()
    rows = E.search(db, category="CCTV")
    assert [r.code for r in rows] == ["UG-CCTV-001"]


def test_검색어는_메시지와_경로를_함께_본다(db):
    _seed()
    assert len(E.search(db, q="스트림")) == 1
    assert len(E.search(db, q="/api/x")) == 1


def test_발생위치와_처리여부로_검색한다(db):
    _seed()
    assert len(E.search(db, source="worker")) == 1
    assert len(E.search(db, resolved="0")) == 3
    assert len(E.search(db, resolved="1")) == 0


def test_요약은_미처리와_심각을_센다(db):
    E.record(code="UG-EXT-003", message="통보 실패", severity="critical")
    E.record(code="UG-CCTV-001", message="스트림 실패", severity="error")
    s = E.summary(db)
    assert s["unresolved"] == 2
    assert s["critical"] == 1
    assert s["occurrences"] == 2


# --- 조치·삭제 ---------------------------------------------------------------
def test_처리완료로_표시하고_해제한다(db):
    rid = E.record(message="아무거나")
    assert E.resolve(db, rid, by="admin", note="회선 교체함") is True
    db.expire_all()
    row = db.get(ErrorLog, rid)
    assert row.resolved is True and row.resolved_by == "admin"
    assert row.resolve_note == "회선 교체함"

    E.resolve(db, rid, undo=True)
    db.expire_all()
    assert db.get(ErrorLog, rid).resolved is False


def test_삭제는_실제로_지운다(db):
    a = E.record(message="첫째", path="/a")
    b = E.record(message="둘째", path="/b")
    assert E.delete(db, [a]) == 1
    db.expire_all()
    assert db.get(ErrorLog, a) is None
    assert db.get(ErrorLog, b) is not None


def test_조건_없는_전체_삭제는_거부한다(db):
    E.record(message="아무거나")
    with pytest.raises(ValueError):
        E.delete_by_filter(db, resolved_only=False, before_days=0)
    assert len(E.search(db)) == 1


def test_일괄_정리는_기본으로_처리완료_건만_지운다(db):
    keep = E.record(message="미처리", path="/a")
    gone = E.record(message="처리함", path="/b")
    E.resolve(db, gone, by="admin")
    for rid in (keep, gone):
        row = db.get(ErrorLog, rid)
        row.last_seen_at = row.last_seen_at - timedelta(days=40)
    db.commit()

    assert E.delete_by_filter(db, before_days=30) == 1
    db.expire_all()
    assert db.get(ErrorLog, keep) is not None, "미처리 오류가 함께 지워졌다"
    assert db.get(ErrorLog, gone) is None


# --- 오류 코드 사전 ----------------------------------------------------------
def test_기본_코드를_심는다(db):
    n = E.seed_builtin(db)
    assert n == len(catalog.BUILTIN)
    assert E.get_code(db, catalog.UNKNOWN) is not None


def test_다시_심어도_고친_내용을_덮어쓰지_않는다(db):
    """배포할 때마다 현장에서 적은 조치 방법이 사라지면 안 된다."""
    E.seed_builtin(db)
    row = E.get_code(db, "UG-CCTV-001")
    row.resolution = "우리 현장 조치법"
    db.commit()

    assert E.seed_builtin(db) == 0
    db.expire_all()
    assert E.get_code(db, "UG-CCTV-001").resolution == "우리 현장 조치법"


def test_코드를_등록하고_수정한다(db):
    row = E.save_code(db, code="ug-test-001", category="SYS", title="시험용",
                      severity="warn", cause="원인", resolution="조치", by="admin")
    assert row.code == "UG-TEST-001", "코드는 대문자로 정규화돼야 한다"
    assert row.builtin is False

    again = E.save_code(db, code="UG-TEST-001", category="SYS", title="고침",
                        severity="error", cause="c", resolution="r", by="admin")
    assert again.id == row.id and again.title == "고침"


@pytest.mark.parametrize("bad", [
    {"code": "잘못된코드"},
    {"category": "없는분류"},
    {"severity": "매우심각"},
    {"title": "   "},
])
def test_잘못된_입력은_거부한다(db, bad):
    kw = {"code": "UG-TEST-002", "category": "SYS", "title": "제목",
          "severity": "error", "cause": "", "resolution": ""}
    kw.update(bad)
    with pytest.raises(ValueError):
        E.save_code(db, **kw)


def test_기본_제공_코드는_삭제할_수_없다(db):
    E.seed_builtin(db)
    with pytest.raises(ValueError):
        E.delete_code(db, "UG-CCTV-001")
    assert E.get_code(db, "UG-CCTV-001") is not None


def test_직접_등록한_코드는_삭제할_수_있다(db):
    E.save_code(db, code="UG-TEST-003", category="SYS", title="t",
                severity="error", cause="", resolution="")
    assert E.delete_code(db, "UG-TEST-003") is True
    assert E.get_code(db, "UG-TEST-003") is None


def test_사전에_없는_코드로도_기록된다(db):
    """등록된 코드가 아니라고 오류를 버리면 그 오류는 영원히 안 보인다."""
    rid = E.record(code="UG-NEW-999", message="처음 보는 오류")
    assert db.get(ErrorLog, rid).code == "UG-NEW-999"


def test_코드별_발생_건수를_센다(db):
    E.record(code="UG-CCTV-001", message="a", path="/1")
    E.record(code="UG-CCTV-001", message="b", path="/2")
    E.record(code="UG-DB-001", message="c", path="/3")
    usage = E.code_usage(db)
    assert usage["UG-CCTV-001"] == 2
    assert usage["UG-DB-001"] == 1
