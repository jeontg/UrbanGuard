"""테스트 공통 설정 — 전용 PostgreSQL 데이터베이스와 인증된 클라이언트.

운영과 같은 PostgreSQL로 테스트한다(SQLite 폴백 없음). 방언 차이로 배포
시점에 문제가 드러나는 것을 막기 위해서다. 다만 **개발용 DB와는 다른
데이터베이스**(``urbanguard_test``)를 써서 개발 데이터를 건드리지 않는다.

DB가 떠 있지 않으면 관련 테스트는 명확한 사유와 함께 건너뛴다 —
연결 실패가 엉뚱한 오류로 번지지 않게 하기 위해서다.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from pathlib import Path

import pytest

# --- 서비스 모듈이 임포트되기 **전에** 잡아야 하는 값 -------------------------
#
# ``service/main.py`` 는 임포트 시점에 알림 발송기와 블록 목록을 만든다. 그래서
# 이 값들을 시험 모듈 안에서 잡으면, **그 모듈보다 먼저 main 을 임포트한 다른
# 모듈이 있을 때 아무 효과가 없다.** 실제로 오류 관리 시험(test_error_admin)이
# 이름 순서상 먼저 임포트되면서 알림 시험이 503으로 깨진 적이 있다.
# 모든 시험 모듈보다 먼저 도는 여기에 두어 순서에 상관없이 같게 만든다.
os.environ.setdefault(
    "TOT_BLOCKS_PATH",
    str(Path(__file__).parent / "service" / "fixtures" / "blocks_synthetic.json"))
os.environ.setdefault("NOTIFICATION_DRY_RUN", "true")
os.environ.setdefault("ALERT_RECIPIENTS", "01012345678")

TEST_DB_URL = os.environ.get(
    "URBANGUARD_TEST_DATABASE_URL",
    "postgresql+psycopg://urbanguard:ug_dev_2026@127.0.0.1:5433/urbanguard_test")

# 앱 모듈이 임포트되기 전에 잡아야 엔진이 테스트 DB를 바라본다.
os.environ["URBANGUARD_DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("URBANGUARD_SECRET_KEY", "test-only-not-a-real-secret")
# 증거 영상(S-88)은 **운영 데이터 폴더에 쓰면 안 된다.**
# 기동 훅(lifespan)을 여는 시험이 있으면 탐지 훅이 전역으로 등록돼, 다른
# 시험이 만든 이벤트가 진짜 data/evidence 에 파일을 남긴다. 실제로 남았다.
os.environ.setdefault(
    "URBANGUARD_EVIDENCE_DIR",
    str(pathlib.Path(tempfile.gettempdir()) / "urbanguard_test_evidence"))
os.environ.setdefault("URBANGUARD_ORG_NAME", "테스트시")

ADMIN_ID = "test_admin"
ADMIN_PW = "TestAdmin!2026"
OPR_ID = "test_opr"
OPR_PW = "TestOper!2026"


def _db_available() -> bool:
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(TEST_DB_URL)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


def _do_login(client, login_id: str, password: str) -> None:
    """TestClient 에 세션 쿠키를 심는다."""
    res = client.post("/login",
                      data={"login_id": login_id, "password": password, "next": "/"},
                      follow_redirects=False)
    assert res.status_code == 303, f"로그인 실패: {res.status_code}"


@pytest.fixture(scope="session")
def login():
    """테스트 안에서 다른 계정으로 다시 로그인할 때 쓴다."""
    return _do_login


def _do_login_cross_service(target_client, login_id: str, password: str) -> None:
    """다른 서비스(예: crowd_service.app)의 TestClient에 세션 쿠키를 심는다.

    ⚠️ 2026-08-31 — API 게이트웨이 Phase 1(인파관리 서비스 분리) 이후 신설.
    ``/login``은 platform-shell(``service/main.py``)에만 있고
    ``crowd_service.app``에는 없다 — 실제 배포에서도 로그인은 항상
    platform-shell이 처리하고, 다른 서비스는 그 쿠키를 검증만 한다
    (같은 ``URBANGUARD_SECRET_KEY``를 공유하므로 가능, ``core/security.py``
    참고). 이 헬퍼가 그 흐름을 시험에서 그대로 재현한다 — 별도
    ``TestClient(main.app)``로 로그인해 쿠키만 뽑아 대상 클라이언트에
    옮긴다."""
    from fastapi.testclient import TestClient
    from tot_dashboard.service.main import app as _platform_app

    auth_client = TestClient(_platform_app)
    res = auth_client.post("/login",
                           data={"login_id": login_id, "password": password, "next": "/"},
                           follow_redirects=False)
    assert res.status_code == 303, f"로그인 실패: {res.status_code}"
    target_client.cookies.update(auth_client.cookies)


@pytest.fixture(scope="session")
def login_cross_service():
    """다른 서비스의 TestClient에 platform-shell 로그인 쿠키를 심는다."""
    return _do_login_cross_service


@pytest.fixture(scope="session")
def db_schema():
    """테스트 DB 스키마를 만든다.

    alembic 대신 create_all 을 쓰는 이유는 속도다. 마이그레이션 자체의
    정합성은 개발 DB에 실제로 적용해 확인한다.
    """
    if not _db_available():
        pytest.skip("테스트용 PostgreSQL 미가동")
    from tot_dashboard.core.db import Base, engine, reset_engine
    from tot_dashboard.core import models  # noqa: F401  (테이블 등록)
    reset_engine()
    eng = engine()
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield
    Base.metadata.drop_all(eng)


@pytest.fixture(scope="session")
def seeded_users(db_schema):
    """시스템관리자와 관제요원 계정을 하나씩 만든다."""
    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.roles import Domain, Role
    db = get_session()
    try:
        # must_change=False — 이 계정들은 곧바로 화면을 써야 한다.
        # 임시 비밀번호 강제 변경 흐름은 별도 테스트에서 따로 확인한다.
        create_user(db, login_id=ADMIN_ID, name="테스트관리자", dept="정보통신과",
                    role=Role.SYS.value, password=ADMIN_PW,
                    domains=[d.value for d in Domain], must_change=False)
        create_user(db, login_id=OPR_ID, name="테스트관제", dept="상황실",
                    role=Role.OPR.value, password=OPR_PW, domains=[],
                    must_change=False)
        db.commit()
    finally:
        db.close()
    return {"admin": (ADMIN_ID, ADMIN_PW), "opr": (OPR_ID, OPR_PW)}
