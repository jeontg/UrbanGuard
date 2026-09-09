"""사용자별 화면 설정 (user_prefs)

**왜 필요한가.** 멀티뷰 켜짐·꺼짐을 브라우저(localStorage)에 두었더니,
관제요원이 자리를 옮기면 설정이 사라졌다. 본인은 **껐다고 생각한 것이 켜져
있는** 상태가 된다. 사람에 붙는 값은 사람에 저장해야 한다.

일부러 **키·값 표**로 만들었다. 앞으로 사람마다 다른 설정이 더 생길 때
(화면 정렬, 기본 도메인 등) 표를 또 만들지 않기 위해서다.

신규 표라 기존 데이터를 건드리지 않는다.

Revision ID: a1c7d90e4b52
Revises: d4a0c71b8e35
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c7d90e4b52"
down_revision = "d4a0c71b8e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        # 계정을 지우면 그 사람의 설정도 함께 사라진다 — 남겨 둘 이유가 없고,
        # 남으면 나중에 같은 id 를 받은 사람에게 엉뚱하게 붙는다.
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_user_pref"),
    )
    op.create_index("ix_user_prefs_user", "user_prefs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_prefs_user", table_name="user_prefs")
    op.drop_table("user_prefs")
