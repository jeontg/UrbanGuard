"""PostgreSQL 연결과 세션 관리.

DB는 PostgreSQL만 지원한다(사용자 지정). SQLite 폴백을 두지 않는 이유는,
운영과 다른 DB로 테스트하면 방언 차이(JSONB, 시퀀스, 타임존 처리)가 배포 시점에
드러나기 때문이다. 개발 PC에서도 같은 PostgreSQL을 쓴다.

접속 정보는 환경변수 ``URBANGUARD_DATABASE_URL`` 하나로 결정한다.
개발용 기본값은 .tools/ 아래 로컬 인스턴스를 가리키며, 운영 배포 시에는
반드시 .env 로 덮어써야 한다.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_URL = "postgresql+psycopg://urbanguard:ug_dev_2026@127.0.0.1:5433/urbanguard"


def database_url() -> str:
    return os.environ.get("URBANGUARD_DATABASE_URL", DEFAULT_URL)


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def engine():
    """엔진을 지연 생성한다.

    모듈 임포트 시점에 만들면 DB가 아직 안 떠 있을 때 서비스 기동 자체가 실패한다.
    대시보드는 DB 없이도 탐지 파이프라인은 돌아가야 하므로 지연 생성한다.
    """
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            database_url(),
            pool_pre_ping=True,   # 유휴 연결이 끊긴 뒤의 첫 요청 실패를 막는다
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, class_=Session,
                                     expire_on_commit=False, future=True)
    return _engine


def session_factory():
    engine()
    return _SessionLocal


def get_session() -> Session:
    return session_factory()()


def reset_engine() -> None:
    """테스트가 DB URL을 바꾼 뒤 엔진을 다시 만들게 한다."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
