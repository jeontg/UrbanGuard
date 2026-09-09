"""탐지 피드백(S-07)과 멀티뷰 배치(S-05)

근거 — 경남 SFR-003·SFR-001, 서울 SFR-009·SFR-015.

전부 신규 표라 기존 데이터를 건드리지 않는다.

Revision ID: d4a0c71b8e35
Revises: c8e19a45b072
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d4a0c71b8e35"
down_revision = "c8e19a45b072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "detection_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        # 미탐이면 NULL — 붙일 이벤트가 없다.
        sa.Column("event_id", sa.Integer(),
                  sa.ForeignKey("events.id", ondelete="SET NULL")),
        sa.Column("camera_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("domain", sa.String(16), nullable=False, server_default=""),
        sa.Column("hazard_type_code", sa.String(32), nullable=False,
                  server_default=""),
        sa.Column("level", sa.String(16), nullable=False, server_default=""),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("login_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("used_for_training", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index("ix_feedback_event", "detection_feedback", ["event_id"])
    op.create_index("ix_feedback_camera", "detection_feedback", ["camera_id"])
    op.create_index("ix_feedback_created", "detection_feedback",
                    [sa.text("created_at DESC")])
    op.create_index("ix_feedback_verdict", "detection_feedback", ["verdict"])

    op.create_table(
        "multiview_layouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("tiles", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("cameras", JSONB()),
        sa.Column("is_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "name", name="uq_multiview_layout"),
    )
    op.create_index("ix_multiview_user", "multiview_layouts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_multiview_user", table_name="multiview_layouts")
    op.drop_table("multiview_layouts")
    op.drop_index("ix_feedback_verdict", table_name="detection_feedback")
    op.drop_index("ix_feedback_created", table_name="detection_feedback")
    op.drop_index("ix_feedback_camera", table_name="detection_feedback")
    op.drop_index("ix_feedback_event", table_name="detection_feedback")
    op.drop_table("detection_feedback")
