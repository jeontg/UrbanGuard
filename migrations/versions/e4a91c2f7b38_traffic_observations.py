"""traffic_observations — 교통 관측 이력 (flood/traffic 도메인 분리)

flood/traffic 도메인 분리(docs/202608210801)로 교통이 1급 도메인이 됐다.
교통 판정(강우×정체×정지차량)을 매 틱 계산해 놓고 버리고 있어서 「봤는데
평온했다」가 남지 않았다 — ``crowd_observations`` 를 만든 것과 같은 이유로
먼저 쌓기 시작한다.

⚠️ 이 리비전은 **표만 만든다.** 실제 기록은 이벤트 이중화(5단계) 이후
``service/event_sync.py`` 에서 붙인다.

Revision ID: e4a91c2f7b38
Revises: d2f4a8c916e3
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4a91c2f7b38"
down_revision = "d2f4a8c916e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traffic_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("camera_name", sa.String(length=120), nullable=False,
                  server_default=""),
        sa.Column("rain_mm_h", sa.Float(), nullable=False, server_default="0"),
        # 평상시 대비 감소율(0~1). 0 이 「감소 없음」이라 기본값이 맞다.
        sa.Column("speed_drop", sa.Float(), nullable=False, server_default="0"),
        sa.Column("queue_len", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stalled_count", sa.Integer(), nullable=False,
                  server_default="0"),
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
    op.create_index("ix_traffic_observations_camera_id", "traffic_observations",
                    ["camera_id"])
    op.create_index("ix_traffic_observations_observed_at", "traffic_observations",
                    ["observed_at"])
    # 화면·예측 모두 「이 지점의 최근 구간」을 읽는다.
    op.create_index("ix_traffic_observations_camera_time", "traffic_observations",
                    ["camera_id", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_traffic_observations_camera_time",
                  table_name="traffic_observations")
    op.drop_index("ix_traffic_observations_observed_at",
                  table_name="traffic_observations")
    op.drop_index("ix_traffic_observations_camera_id",
                  table_name="traffic_observations")
    op.drop_table("traffic_observations")
