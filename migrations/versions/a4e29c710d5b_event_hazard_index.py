"""ix_event_domain_block_hazard — 이벤트 유일성 축에 위험유형 추가

교통위험 돌발상황 확장(2026-08-26, `docs/202608260842/`)으로 같은
(도메인, 지점)에서 강우정체·역주행·보행자 등 서로 다른 유형이 동시에
열릴 수 있게 됐다. ``core/events.py::open_event_for()``가 이제
``hazard_type_code``까지 조회 조건에 넣으므로, 그 조합에 인덱스를
추가한다. 기존 ``ix_event_domain_block``은 목록·필터 조회가 여전히
쓰므로 그대로 둔다.

Revision ID: a4e29c710d5b
Revises: 3fafb126d787
"""
from __future__ import annotations

from alembic import op

revision = "a4e29c710d5b"
down_revision = "3fafb126d787"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_event_domain_block_hazard", "events",
        ["domain", "block_id", "hazard_type_code"])


def downgrade() -> None:
    op.drop_index("ix_event_domain_block_hazard", table_name="events")
