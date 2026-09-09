"""training_runs — AI 모델 학습 실행 기록 (신규)

관리자 화면에서 침수·교통위험·인파관리·도로 노면 4개 도메인의 모델 학습을
실행할 수 있게 하며, 그 실행 기록(파라미터·상태·성능 지표)을 남긴다.

``docs/pending_tasks.md`` A-7(★★ 「재학습·MLOps — 성능 기록 표가 아예
없음」)에서 지적된 그 표다.

Revision ID: 3fafb126d787
Revises: e4a91c2f7b38
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3fafb126d787"
down_revision = "e4a91c2f7b38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="queued"),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.String(length=500), nullable=False,
                  server_default=""),
        sa.Column("output_dir", sa.String(length=500), nullable=False,
                  server_default=""),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_by", sa.Integer(), nullable=True),
        sa.Column("started_by_name", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_training_runs_domain", "training_runs", ["domain"])
    op.create_index("ix_training_runs_status", "training_runs", ["status"])
    op.create_index("ix_training_runs_started", "training_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_training_runs_started", table_name="training_runs")
    op.drop_index("ix_training_runs_status", table_name="training_runs")
    op.drop_index("ix_training_runs_domain", table_name="training_runs")
    op.drop_table("training_runs")
