"""events.last_detected_at 추가 — 이벤트 자동 보류 정책 (2026-09-02)

`updated_at`은 20초 주기 동기화 루프가 등급과 무관하게 매번 갱신하고,
사람이 확인/종결만 눌러도 SQLAlchemy onupdate로 같이 갱신돼 "위험이
임계등급 이상으로 다시 관측됐는가"의 근거로 쓸 수 없다(직접 실측 확인,
`core/events.py` 참고). 이 칸은 `record_detection()`이
`is_reportable(level)`일 때만 갱신한다 — "재탐지 없음"을 정확히
판정하기 위한 전용 컬럼.

기존 행은 `detected_at`(최초 생성 시각)으로 채운다 — 비워 두면 옛
이벤트가 전부 "재탐지 없음"으로 즉시 판정돼 자동 보류 정책이 켜지자마자
쏟아지듯 넘어가는 사고를 막기 위함이다(최초 생성 시각을 기준으로 남은
시간을 계산하게 하는 보수적 처리).

Revision ID: a1b2c3d4e5f6
Revises: 222303a5f105
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '222303a5f105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "events",
        sa.Column("last_detected_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.execute(
        "UPDATE events SET last_detected_at = detected_at "
        "WHERE last_detected_at IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("events", "last_detected_at")
