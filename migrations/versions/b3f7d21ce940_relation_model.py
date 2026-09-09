"""관계 모델 (Urban Ontology 1단계)

설계: docs/202608181432/relation_model_design.md
판단: docs/202608181432/tech_adoption_judgment.md

**전부 「추가」뿐이다.** 기존 행을 고치거나 컬럼을 지우는 단계가 하나도 없다.
비어 있는 채로 두면 기존 동작이 그대로라, 회귀 위험이 없다.

⚠️ ``ltree`` 확장을 쓰지 않는다. ``zones.path`` 는 ``varchar`` 이고 계층 질의는
``LIKE 'prefix.%'`` 로 한다. 설계서에서는 ``ltree`` 를 쓰기로 했으나 그러면
**폐쇄망 납품에서 설치 대상이 하나 늘어난다.** 경남이 도 + 18개 시·군이라
행 수가 수백 단위여서 성능 차이가 없다.

Revision ID: b3f7d21ce940
Revises: a1c47f30d9b2
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3f7d21ce940"
down_revision = "a1c47f30d9b2"
branch_labels = None
depends_on = None


# --- 어휘 초기값 ------------------------------------------------------------
#
# ★ 2026-08-22 — 예전에는 ``core/vocabulary.py`` 를 **런타임에 임포트**했다
#   ("목록은 한 곳에만 있어야 한다"는 이유였다). 그런데 마이그레이션은
#   **그 시점의 스키마를 재현하는 기록**이라, 실행 시점의 코드를 끌어다
#   쓰면 두 가지가 깨진다.
#
#     ① 신규 설치에서 ``alembic upgrade`` 가 앱 패키지 임포트에 의존한다 —
#        vocabulary.py 가 새 모듈을 임포트하게 되는 순간 마이그레이션이
#        통째로 실패한다(DB 마이그레이션이 앱 코드보다 먼저 돌아야 하는
#        배포에서 특히 위험).
#     ② 나중에 어휘가 늘면(실제로 2026-08-21 도메인 분리에서 traffic 유형이
#        추가됐다) **과거 마이그레이션이 과거에 없던 값을 심는다.**
#
#   그래서 ``b3e5f1a72c04`` 가 이미 쓰는 원칙("값을 하드코딩해 패키지 임포트
#   없이 돌게 한다")으로 통일한다.
#
#   ⚠️ 이 목록이 ``core/vocabulary.py`` 와 갈라져도 문제되지 않는다 —
#      서비스가 뜰 때마다 ``vocabulary.seed_builtin()`` 이 **멱등하게**
#      다시 심어 최신 어휘를 채운다(``service/main.py`` 기동 훅). 여기 값은
#      「그 시점의 초기값」일 뿐이다.
_RISK_LEVELS = [
    ('interest', 1, '관심', '#2f9e44', False),
    ('caution', 2, '주의', '#f59f00', False),
    ('alert', 3, '경계', '#e8590c', False),
    ('severe', 4, '심각', '#c92a2a', True),
]
_HAZARD_TYPES = [
    ('flood', 'flood', None, '침수', 'own', True),
    ('flood_underpass', 'flood', 'flood', '지하차도 침수', 'rfp', True),
    ('flood_drainage', 'flood', 'flood', '배수로 침수', 'rfp', True),
    ('flood_river', 'flood', 'flood', '하천 범람', 'rfp', False),
    ('traffic', 'traffic', None, '교통', 'own', True),
    ('traffic_rain_congestion', 'traffic', 'traffic', '강우 정체', 'own', True),
    ('traffic_stalled_vehicle', 'traffic', 'traffic', '정지·고착 차량', 'own', True),
    ('traffic_queue_delay', 'traffic', 'traffic', '대기열 지연', 'own', True),
    ('traffic_impassable', 'traffic', 'traffic', '통행 불가', 'own', True),
    ('crowd', 'crowd', None, '인파', 'own', True),
    ('crowd_density', 'crowd', 'crowd', '인파 밀집', 'own', True),
    ('crowd_loitering', 'crowd', 'crowd', '배회', 'own', True),
    ('road', 'road', None, '노면', 'own', True),
    ('road_pothole', 'road', 'road', '노면 파손', 'own', True),
    ('wildfire', '', None, '산불 확산', 'rfp', False),
    ('typhoon_damage', '', None, '태풍 피해', 'rfp', False),
]


def upgrade() -> None:
    # --- 1) 어휘 --------------------------------------------------------
    op.create_table(
        "risk_levels",
        sa.Column("code", sa.String(16), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("color", sa.String(16), nullable=False, server_default=""),
        sa.Column("is_critical", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("std_uri", sa.String(256), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )
    op.create_table(
        "hazard_types",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("domain", sa.String(16), nullable=False, server_default=""),
        sa.Column("parent_code", sa.String(32),
                  sa.ForeignKey("hazard_types.code", ondelete="SET NULL")),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="own"),
        sa.Column("detectable", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("std_uri", sa.String(256), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )
    op.create_index("ix_hazard_types_domain", "hazard_types", ["domain"])
    op.create_index("ix_hazard_types_parent", "hazard_types", ["parent_code"])

    # --- 2) 개체 --------------------------------------------------------
    op.create_table(
        "zones",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("path", sa.String(512), nullable=False, server_default=""),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("hazard_type_code", sa.String(32),
                  sa.ForeignKey("hazard_types.code", ondelete="SET NULL")),
        sa.Column("source", sa.String(32), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )
    op.create_index("ix_zones_path", "zones", ["path"])
    op.create_index("ix_zones_kind", "zones", ["kind"])

    op.create_table(
        "sensors",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("lat", sa.Float()),
        sa.Column("lng", sa.Float()),
        sa.Column("source", sa.String(32), nullable=False, server_default=""),
        sa.Column("external_id", sa.String(128), nullable=False,
                  server_default=""),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )

    # --- 3) 관계 --------------------------------------------------------
    op.create_table(
        "camera_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("from_camera_id", sa.String(64),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("to_camera_id", sa.String(64),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(16), nullable=False,
                  server_default="adjacent"),
        sa.Column("distance_m", sa.Integer()),
        sa.Column("bearing_deg", sa.Integer()),
        sa.Column("auto", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("from_camera_id", "to_camera_id", "kind",
                            name="uq_camera_link"),
    )
    op.create_index("ix_camera_links_from", "camera_links",
                    ["from_camera_id", "kind"])
    op.create_index("ix_camera_links_to", "camera_links", ["to_camera_id"])

    op.create_table(
        "camera_sensors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("camera_id", sa.String(64),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("sensor_id", sa.String(64),
                  sa.ForeignKey("sensors.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(16), nullable=False,
                  server_default="reference"),
        sa.Column("distance_m", sa.Integer()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("camera_id", "sensor_id", name="uq_camera_sensor"),
    )
    op.create_index("ix_camera_sensors_camera", "camera_sensors", ["camera_id"])
    op.create_index("ix_camera_sensors_sensor", "camera_sensors", ["sensor_id"])

    op.create_table(
        "camera_zones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("camera_id", sa.String(64),
                  sa.ForeignKey("cameras.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("zone_id", sa.String(64),
                  sa.ForeignKey("zones.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("coverage", sa.String(16), nullable=False,
                  server_default="partial"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("camera_id", "zone_id", name="uq_camera_zone"),
    )
    op.create_index("ix_camera_zones_zone", "camera_zones", ["zone_id"])

    op.create_table(
        "hazard_sop_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hazard_type_code", sa.String(32),
                  sa.ForeignKey("hazard_types.code", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("level_code", sa.String(16), nullable=False,
                  server_default=""),
        sa.Column("zone_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("sop_step_id", sa.Integer(),
                  sa.ForeignKey("sop_steps.id", ondelete="CASCADE")),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("hazard_type_code", "level_code", "zone_id",
                            "sop_step_id", name="uq_hazard_sop"),
    )
    op.create_index("ix_hazard_sop_lookup", "hazard_sop_map",
                    ["hazard_type_code", "level_code"])

    # --- 4) 기존 표에 nullable 추가 ------------------------------------
    #
    # 전부 NULL 허용이라 기존 39지점 행을 건드리지 않는다.
    op.add_column("cameras", sa.Column("bearing_deg", sa.Integer()))
    op.add_column("cameras", sa.Column("tilt_deg", sa.Integer()))
    op.add_column("cameras", sa.Column("fov_deg", sa.Integer()))
    op.add_column("cameras", sa.Column("purpose", sa.String(32), nullable=False,
                                       server_default=""))
    op.add_column("events", sa.Column("hazard_type_code", sa.String(32)))

    # --- 5) 어휘 초기 행 -------------------------------------------------
    #
    # 부모를 먼저 넣어야 자기참조 FK 가 걸리지 않는다. _HAZARD_TYPES 목록이
    # 이미 부모 → 자식 순서다.
    rl = sa.table(
        "risk_levels",
        sa.column("code", sa.String), sa.column("seq", sa.Integer),
        sa.column("label", sa.String), sa.column("color", sa.String),
        sa.column("is_critical", sa.Boolean))
    op.bulk_insert(rl, [
        {"code": c, "seq": s, "label": lb, "color": col, "is_critical": crit}
        for c, s, lb, col, crit in _RISK_LEVELS])

    ht = sa.table(
        "hazard_types",
        sa.column("code", sa.String), sa.column("domain", sa.String),
        sa.column("parent_code", sa.String), sa.column("label", sa.String),
        sa.column("source", sa.String), sa.column("detectable", sa.Boolean))
    op.bulk_insert(ht, [
        {"code": c, "domain": d, "parent_code": p, "label": lb,
         "source": src, "detectable": det}
        for c, d, p, lb, src, det in _HAZARD_TYPES])


def downgrade() -> None:
    op.drop_column("events", "hazard_type_code")
    op.drop_column("cameras", "purpose")
    op.drop_column("cameras", "fov_deg")
    op.drop_column("cameras", "tilt_deg")
    op.drop_column("cameras", "bearing_deg")

    op.drop_index("ix_hazard_sop_lookup", table_name="hazard_sop_map")
    op.drop_table("hazard_sop_map")
    op.drop_index("ix_camera_zones_zone", table_name="camera_zones")
    op.drop_table("camera_zones")
    op.drop_index("ix_camera_sensors_sensor", table_name="camera_sensors")
    op.drop_index("ix_camera_sensors_camera", table_name="camera_sensors")
    op.drop_table("camera_sensors")
    op.drop_index("ix_camera_links_to", table_name="camera_links")
    op.drop_index("ix_camera_links_from", table_name="camera_links")
    op.drop_table("camera_links")
    op.drop_table("sensors")
    op.drop_index("ix_zones_kind", table_name="zones")
    op.drop_index("ix_zones_path", table_name="zones")
    op.drop_table("zones")
    op.drop_index("ix_hazard_types_parent", table_name="hazard_types")
    op.drop_index("ix_hazard_types_domain", table_name="hazard_types")
    op.drop_table("hazard_types")
    op.drop_table("risk_levels")
