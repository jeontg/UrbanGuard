"""event_evidence 탐지 이벤트 증거 자료 (S-88)

이벤트에 「무슨 일이 있었는가」를 보여 주는 정지영상·클립을 붙인다.
파일 자체가 개인정보이므로 열람·삭제는 권한과 감사 로그로 막는다.

Revision ID: f9c41a6b8d33
Revises: e7b3c9014f2a
Create Date: 2026-08-16 21:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f9c41a6b8d33'
down_revision: Union[str, Sequence[str], None] = 'e7b3c9014f2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "event_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("domain", sa.String(length=16), nullable=False,
                  server_default=""),
        # 수집 당시 등급. 이벤트 등급은 나중에 바뀌므로 그때 값을 굳혀 둔다.
        sa.Column("level", sa.String(length=16), nullable=False,
                  server_default=""),

        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        # 「제출한 파일이 그때 그 파일이 맞다」를 말하기 위한 해시.
        sa.Column("sha256", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("duration_sec", sa.Integer(), nullable=False,
                  server_default="0"),

        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),

        sa.PrimaryKeyConstraint("id"),
        # 이벤트를 지우면 증거도 함께 지운다 — 근거 없는 파일만 남으면
        # 그것이 곧 목적 없는 개인정보 보관이 된다.
        sa.ForeignKeyConstraint(["event_id"], ["events.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_evidence_event", "event_evidence", ["event_id"])
    op.create_index("ix_evidence_level", "event_evidence", ["level"])
    op.create_index("ix_evidence_captured", "event_evidence",
                    [sa.text("captured_at DESC")])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_evidence_captured", table_name="event_evidence")
    op.drop_index("ix_evidence_level", table_name="event_evidence")
    op.drop_index("ix_evidence_event", table_name="event_evidence")
    op.drop_table("event_evidence")
