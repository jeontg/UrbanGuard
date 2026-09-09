"""등급 구간 표 (level_thresholds)

침수 단계 어휘를 4등급으로 통일하면서, **기관이 구간을 바꿀 수 있도록**
숫자를 코드에서 표로 뺐다(S-95 위험등급 관리).

⚠️ 초기 행은 ``core/vocabulary.py`` 의 ``DEFAULT_THRESHOLDS`` 하나에서만
온다. 여기에 복사해 두면 둘이 갈라진다.

Revision ID: c8e19a45b072
Revises: b3f7d21ce940
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# ★ 2026-08-22 — ``core/vocabulary.py`` 런타임 임포트를 없앴다. 이유는
#   ``b3f7d21ce940`` 의 같은 자리 주석 참고(신규 설치에서 alembic 이 앱
#   패키지에 의존하면 안 되고, 과거 마이그레이션이 과거에 없던 값을 심으면
#   안 된다). 서비스 기동 시 ``vocabulary.seed_builtin()`` 이 멱등하게 다시
#   심으므로 이 목록이 최신과 갈라져도 문제되지 않는다.
DEFAULT_THRESHOLDS = [
    ('flood', 'caution', 5.0, 'cm', '행안부 지하차도 통제 기준(15→5cm 강화)'),
    ('flood', 'alert', 15.0, 'cm', '차량 접지력 상실 시작 (NWS/FEMA)'),
    ('flood', 'severe', 30.0, 'cm', '소형차 부유 시작 (NWS/FEMA)'),
    ('crowd', 'caution', 3.0, '명/㎡', '혼잡 시작'),
    ('crowd', 'alert', 4.0, '명/㎡', '영국 이동 대기열 한계'),
    ('crowd', 'severe', 5.0, '명/㎡', '국제 압사 임계'),
    ('road', 'road_watch', 1.0, '건/100m', '구간 보정 후 손상이 보이기 시작'),
    ('road', 'road_repair', 3.0, '건/100m', '보수 계획 수립 권고'),
    ('road', 'road_urgent', 6.0, '건/100m', '즉시 보수 대상'),
]

revision = "c8e19a45b072"
down_revision = "b3f7d21ce940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "level_thresholds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(16), nullable=False),
        sa.Column("level_code", sa.String(16),
                  sa.ForeignKey("risk_levels.code", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("min_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(16), nullable=False, server_default=""),
        sa.Column("source_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_by", sa.String(64), nullable=False,
                  server_default=""),
        sa.UniqueConstraint("domain", "level_code", name="uq_level_threshold"),
    )
    op.create_index("ix_level_thresholds_domain", "level_thresholds", ["domain"])

    tb = sa.table(
        "level_thresholds",
        sa.column("domain", sa.String), sa.column("level_code", sa.String),
        sa.column("min_value", sa.Float), sa.column("unit", sa.String),
        sa.column("source_note", sa.Text), sa.column("updated_by", sa.String))
    op.bulk_insert(tb, [
        {"domain": d, "level_code": c, "min_value": v, "unit": u,
         "source_note": n, "updated_by": "system"}
        for d, c, v, u, n in DEFAULT_THRESHOLDS])


def downgrade() -> None:
    op.drop_index("ix_level_thresholds_domain", table_name="level_thresholds")
    op.drop_table("level_thresholds")
