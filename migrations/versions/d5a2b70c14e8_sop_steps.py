"""sop_steps · event_sop_checks 디지털 SOP (S-86 · S-03)

화면이 「경계입니다」까지만 말하고 그다음을 근무자 기억에 맡기던 것을,
기관 매뉴얼의 조치 순서로 대신하게 한다.

Revision ID: d5a2b70c14e8
Revises: c3f81d24e9a7
Create Date: 2026-08-16 16:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5a2b70c14e8'
down_revision: Union[str, Sequence[str], None] = 'c3f81d24e9a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sop_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        # 빈 문자열이면 「전체」. 도메인·등급과 무관한 공통 단계를 표현한다.
        sa.Column("domain", sa.String(length=16), nullable=False,
                  server_default=""),
        sa.Column("level", sa.String(length=16), nullable=False,
                  server_default=""),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("required", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("builtin", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sop_scope", "sop_steps", ["domain", "level", "seq"])

    op.create_table(
        "event_sop_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer()),
        # 단계가 지워져도 「무엇을 했는지」는 남아야 한다.
        sa.Column("step_title", sa.String(length=160), nullable=False,
                  server_default=""),
        sa.Column("user_id", sa.Integer()),
        sa.Column("login_id", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("skipped", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["sop_steps.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("event_id", "step_id", name="uq_event_sop_step"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("event_sop_checks")
    op.drop_index("ix_sop_scope", table_name="sop_steps")
    op.drop_table("sop_steps")
