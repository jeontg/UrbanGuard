"""증거 자료 — 이벤트가 난 위치(boxes)

**왜 필요한가.** S-88 팝업이 「이벤트가 발생한 부분」을 보여 달라는 요구를
받았다. 감시 구역(ROI)만으로는 「이 구역을 보고 있었다」까지만 말할 수 있고
「여기서 났다」는 말할 수 없다. **실제 탐지값**을 남겨야 한다.

★ **지어낼 수 없는 것은 비워 둔다.** `boxes` 를 nullable 로 둔 이유 —
그 틱에 물 픽셀이 없었다, 밀집도만으로 뜬 이벤트라 특정 사람이 없다 등
「없는 경우」가 실제로 있다. 없으면 `NULL` 이지 빈 자리채움이 아니다.

기존 행은 전부 `boxes IS NULL` 로 남는다 — 예전에 수집한 자료는 상자 없이
그대로 보여진다(화면이 「없다」고 말한다). 재수집하지 않는다.

Revision ID: d2f4a8c916e3
Revises: c5b8e2f31d47
Create Date: 2026-08-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d2f4a8c916e3"
down_revision = "c5b8e2f31d47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_evidence", sa.Column("boxes", JSONB(), nullable=True))
    op.add_column("event_evidence",
                  sa.Column("frame_w", sa.Integer(), nullable=True))
    op.add_column("event_evidence",
                  sa.Column("frame_h", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("event_evidence", "frame_h")
    op.drop_column("event_evidence", "frame_w")
    op.drop_column("event_evidence", "boxes")
