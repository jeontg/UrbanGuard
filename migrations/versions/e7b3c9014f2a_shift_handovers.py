"""shift_handovers 교대 인수인계 (S-04)

관제는 24시간 이어지는데 사람은 8~12시간마다 바뀐다. 그 이음매에
아무것도 없으면 야간에 열린 이벤트가 아무도 안 보는 채로 남는다.

Revision ID: e7b3c9014f2a
Revises: d5a2b70c14e8
Create Date: 2026-08-16 16:55:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e7b3c9014f2a'
down_revision: Union[str, Sequence[str], None] = 'd5a2b70c14e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "shift_handovers",
        sa.Column("id", sa.Integer(), nullable=False),

        sa.Column("shift_date", sa.DateTime(timezone=True), nullable=False),
        # 조 편성(2교대·3교대·명칭)은 기관마다 달라 목록으로 고정하지 않는다.
        sa.Column("shift_name", sa.String(length=32), nullable=False,
                  server_default=""),

        sa.Column("from_user_id", sa.Integer()),
        sa.Column("from_login", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("to_user_id", sa.Integer()),
        sa.Column("to_login", sa.String(length=64), nullable=False,
                  server_default=""),

        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("todo", sa.Text(), nullable=False, server_default=""),

        # 작성 시점의 미처리·처리중 이벤트 스냅샷. 지금 조회하지 않고 굳혀
        # 두는 이유는, 나중에 종결돼도 「인계 시점에 무엇이 열려 있었나」가
        # 남아야 하기 때문이다.
        sa.Column("open_events", postgresql.JSONB(astext_type=sa.Text())),

        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="draft"),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("ack_note", sa.Text(), nullable=False, server_default=""),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),

        sa.PrimaryKeyConstraint("id"),
        # 계정이 지워져도 인계 기록은 남아야 한다.
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_handover_date", "shift_handovers",
                    [sa.text("shift_date DESC")])
    op.create_index("ix_handover_status", "shift_handovers", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_handover_status", table_name="shift_handovers")
    op.drop_index("ix_handover_date", table_name="shift_handovers")
    op.drop_table("shift_handovers")
