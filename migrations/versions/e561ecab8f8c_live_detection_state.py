"""live_detection_state — 카메라별 「지금」 판정의 크로스 프로세스 조회
(API 게이트웨이 Phase 4, 침수·교통위험 완전 분리)

침수·교통위험이 별도 프로세스(flood-service·traffic-service)로 분리되면
platform-shell의 홈 화면이 더 이상 그 프로세스들의 인메모리 상태를
직접 읽을 수 없다 — 인파(``crowd_observations``)·노면
(``road_inspections``의 최신 1건)이 이미 겪어 해결한 것과 같은 문제다.

``traffic_observations``(이력, 20초 주기)와는 다른 목적이다. 이 표는
이력을 남기지 않고 카메라·도메인당 **행 하나만** 계속 덮어쓴다(1초
주기 upsert 예정) — 홈 화면이 필요한 것은 "지금 등급"뿐이다.

Revision ID: e561ecab8f8c
Revises: a4e29c710d5b
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e561ecab8f8c"
down_revision = "a4e29c710d5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_detection_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        # "flood" | "traffic" — 자유 문자열(핵심 코드 아님, 조회 키일 뿐).
        sa.Column("domain", sa.String(length=16), nullable=False),
        # 관심/주의/경계/심각, 또는 판정이 없으면 빈 문자열.
        sa.Column("level", sa.String(length=8), nullable=False,
                  server_default=""),
        # 이번 틱에 실제로 판정이 돌았는가(water_available/traffic_enabled에
        # 해당). False면 등급이 있어도 화면이 "관측 없음"으로 걸러야 한다.
        sa.Column("available", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    # 도메인별 최신 1건만 있으면 되므로 (카메라, 도메인) 조합은 유일하다 —
    # 매 upsert가 새 행을 쌓는 게 아니라 이 하나를 계속 덮어쓴다.
    op.create_unique_constraint(
        "uq_live_detection_state_camera_domain", "live_detection_state",
        ["camera_id", "domain"])
    op.create_index("ix_live_detection_state_updated_at",
                    "live_detection_state", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_live_detection_state_updated_at",
                  table_name="live_detection_state")
    op.drop_constraint("uq_live_detection_state_camera_domain",
                       "live_detection_state", type_="unique")
    op.drop_table("live_detection_state")
