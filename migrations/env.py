"""Alembic 환경 설정.

접속 URL은 alembic.ini 가 아니라 애플리케이션과 같은 경로(.env 의
URBANGUARD_DATABASE_URL)에서 읽는다. 두 곳에 URL이 흩어지면 마이그레이션을
개발 DB에, 앱을 운영 DB에 붙이는 사고가 난다.
"""
from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

from tot_dashboard.core import models  # noqa: F401,E402  (테이블 등록 목적)
from tot_dashboard.core.db import Base, database_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # client_encoding 명시 이유는 core/db.py::engine()의 같은 인자 주석 참고
    # — 다른 PC의 initdb가 SQL_ASCII로 잡히면 psycopg가 bytes를 돌려줘
    # SQLAlchemy가 서버 버전 파싱 중 TypeError로 죽는 문제를 막는다.
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
        connect_args={"client_encoding": "utf8"})
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
