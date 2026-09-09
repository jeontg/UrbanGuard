"""crowd_observations — 인파 관측 이력

흐름 지표를 매 프레임 계산해 놓고 버리고 있었다. 시계열 예측은 과거가 있어야
가능하므로 **먼저 쌓기 시작한다.**

Revision ID: a1c47f30d9b2
Revises: f9c41a6b8d33
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c47f30d9b2"
down_revision = "f9c41a6b8d33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crowd_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("camera_name", sa.String(length=120), nullable=False,
                  server_default=""),
        sa.Column("person_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("density_index", sa.Float(), nullable=False,
                  server_default="0"),
        sa.Column("mean_speed", sa.Float(), nullable=False, server_default="0"),
        # 1.0 이 평상시. 0 을 기본으로 두면 「속도가 0배」가 되어 통계가 뒤집힌다.
        sa.Column("surge", sa.Float(), nullable=False, server_default="1"),
        sa.Column("dispersion", sa.Float(), nullable=False, server_default="0"),
        sa.Column("divergence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("risk_code", sa.String(length=32), nullable=False,
                  server_default=""),
        sa.Column("risk_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("severity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("drivers", sa.String(length=200), nullable=False,
                  server_default=""),
        sa.Column("failed", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("source", sa.String(length=24), nullable=False,
                  server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crowd_observations_camera_id", "crowd_observations",
                    ["camera_id"])
    op.create_index("ix_crowd_observations_observed_at", "crowd_observations",
                    ["observed_at"])
    # 화면·예측 모두 「이 지점의 최근 구간」을 읽는다.
    op.create_index("ix_crowd_observations_camera_time", "crowd_observations",
                    ["camera_id", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_crowd_observations_camera_time",
                  table_name="crowd_observations")
    op.drop_index("ix_crowd_observations_observed_at",
                  table_name="crowd_observations")
    op.drop_index("ix_crowd_observations_camera_id",
                  table_name="crowd_observations")
    op.drop_table("crowd_observations")
