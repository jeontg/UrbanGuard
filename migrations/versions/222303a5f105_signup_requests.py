"""signup_requests 가입 신청 (2026-09-01)

로그인 화면의 셀프서비스 가입 신청 → 관리자 승인 흐름의 상태를 담는다.
notifications(S-50 알림 승인)와 같은 모양의 요청/승인 표.

Revision ID: 222303a5f105
Revises: e561ecab8f8c
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '222303a5f105'
down_revision: Union[str, Sequence[str], None] = 'e561ecab8f8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "signup_requests",
        sa.Column("id", sa.Integer(), nullable=False),

        sa.Column("login_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("dept", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("requested_role", sa.String(length=8), nullable=False,
                  server_default="OPR"),
        # 콤마 구분 도메인 값 목록. UserDomain처럼 조인 테이블로 안 만드는
        # 이유는 마이그레이션 설명 그대로 core/models.py::SignupRequest
        # docstring 참고 — 승인 전까지는 참고 정보일 뿐이다.
        sa.Column("requested_domains", sa.String(length=255), nullable=False,
                  server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("pw_hash", sa.String(length=255), nullable=False),

        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="pending"),
        sa.Column("ip", sa.String(length=64), nullable=False,
                  server_default=""),

        sa.Column("reviewed_by", sa.Integer()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reject_reason", sa.Text(), nullable=False,
                  server_default=""),
        sa.Column("created_user_id", sa.Integer()),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),

        sa.PrimaryKeyConstraint("id"),
        # 신청 기록은 계정이 지워져도 남아야 한다.
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_user_id"], ["users.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_signup_requests_status", "signup_requests",
                    ["status"])
    op.create_index("ix_signup_requests_login_status", "signup_requests",
                    ["login_id", "status"])
    op.create_index("ix_signup_requests_ip_created", "signup_requests",
                    ["ip", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_signup_requests_ip_created",
                  table_name="signup_requests")
    op.drop_index("ix_signup_requests_login_status",
                  table_name="signup_requests")
    op.drop_index("ix_signup_requests_status", table_name="signup_requests")
    op.drop_table("signup_requests")
