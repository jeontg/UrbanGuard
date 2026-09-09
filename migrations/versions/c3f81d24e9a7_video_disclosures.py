"""video_disclosures 영상 반출 관리대장 (S-94)

개인정보 보호법과 지자체 통합관제센터 운영 규정이 요구하는 기록.
감사 로그와 같은 원칙으로 **지우지 않는다** — 정정은 새 줄 + 사유.

Revision ID: c3f81d24e9a7
Revises: a1c7e94b52d0
Create Date: 2026-08-16 15:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3f81d24e9a7'
down_revision: Union[str, Sequence[str], None] = 'a1c7e94b52d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "video_disclosures",
        sa.Column("id", sa.Integer(), nullable=False),

        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requester_org", sa.String(length=128), nullable=False),
        sa.Column("requester_name", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("requester_contact", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("legal_basis", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),

        sa.Column("camera_ids", sa.Text(), nullable=False, server_default=""),
        sa.Column("period_from", sa.DateTime(timezone=True)),
        sa.Column("period_to", sa.DateTime(timezone=True)),

        sa.Column("method", sa.String(length=16), nullable=False,
                  server_default="view"),
        sa.Column("masked", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("handled_at", sa.DateTime(timezone=True)),
        sa.Column("handler_id", sa.Integer()),
        sa.Column("handler_login", sa.String(length=64), nullable=False,
                  server_default=""),

        sa.Column("disposal_due", sa.DateTime(timezone=True)),
        sa.Column("disposed_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),

        sa.Column("corrects_id", sa.Integer()),
        sa.Column("correction_reason", sa.Text(), nullable=False,
                  server_default=""),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by", sa.String(length=64), nullable=False,
                  server_default=""),

        sa.PrimaryKeyConstraint("id"),
        # 담당자가 지워져도 대장은 남아야 한다.
        sa.ForeignKeyConstraint(["handler_id"], ["users.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["corrects_id"], ["video_disclosures.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_disclosure_requested", "video_disclosures",
                    [sa.text("requested_at DESC")])
    op.create_index("ix_disclosure_org", "video_disclosures", ["requester_org"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_disclosure_org", table_name="video_disclosures")
    op.drop_index("ix_disclosure_requested", table_name="video_disclosures")
    op.drop_table("video_disclosures")
