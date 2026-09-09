"""등급의 성격(kind) 과 노면 정비 등급

**왜 필요한가.** 노면 구간값(1·3·6 건/100m)이 ``core/calibration.py`` 에
상수로 박혀 있어 **기관이 화면에서 바꿀 수 없었다.** 침수·인파는 S-95 에서
바꿀 수 있는데 노면만 못 바꿨다.

★ **그런데 그냥 같은 표에 넣으면 안 된다.** 노면 「양호·관찰·보수 필요·긴급」은
**위험등급이 아니라 정비 등급**이다. 「긴급」은 「지금 통제하라」가 아니라
「빨리 보수하라」다. 섞어 세면 상황판에서 침수 「심각」과 노면 「긴급」 중
무엇이 더 급한지 알 수 없게 된다.

그래서 ``risk_levels`` 에 **성격(kind)** 칸을 두고, 정비 등급은
``maintenance`` 로 넣어 **이벤트 등급 비교·경보 판정에서 빠지게** 한다.
구간 숫자는 화면에서 바꾸되, 쓰이는 자리는 분리한다.

기존 행은 전부 ``risk`` 다 — 판정이 바뀌지 않는다.

Revision ID: c5b8e2f31d47
Revises: b3e5f1a72c04
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c5b8e2f31d47"
down_revision = "b3e5f1a72c04"
branch_labels = None
depends_on = None

# 노면 정비 등급. code 에 접두사를 붙여 **위험등급과 겹치지 않게** 한다 —
# 겹치면 「긴급」과 「심각」이 같은 행을 가리켜 통일한 셈이 된다.
ROAD_LEVELS = [
    ("road_good", 1, "양호", "#2f9e44"),
    ("road_watch", 2, "관찰", "#f59f00"),
    ("road_repair", 3, "보수 필요", "#e8590c"),
    ("road_urgent", 4, "긴급", "#c92a2a"),
]

# 지금 코드에 박혀 있던 값 그대로 옮긴다 — **판정을 바꾸지 않는다.**
# 값을 손보는 것은 기관이 화면에서 할 일이지 마이그레이션이 할 일이 아니다.
ROAD_THRESHOLDS = [
    ("road_watch", 1.0, "건/100m", "구간 보정 후 손상이 보이기 시작"),
    ("road_repair", 3.0, "건/100m", "보수 계획 수립 권고"),
    ("road_urgent", 6.0, "건/100m", "즉시 보수 대상"),
]


def upgrade() -> None:
    # 기존 행은 전부 위험등급이다. server_default 로 채운 뒤 NOT NULL 을 건다 —
    # 순서를 바꾸면 기존 행이 NULL 이라 제약이 걸리지 않는다.
    op.add_column("risk_levels",
                  sa.Column("kind", sa.String(16), nullable=False,
                            server_default="risk"))

    conn = op.get_bind()
    for code, seq, label, color in ROAD_LEVELS:
        # 이미 있으면 건드리지 않는다 — 기관이 이름을 바꿔 놨을 수 있다.
        conn.execute(sa.text(
            "INSERT INTO risk_levels "
            "(code, kind, seq, label, color, is_critical, std_uri, is_active) "
            "VALUES (:c, 'maintenance', :s, :l, :col, false, '', true) "
            "ON CONFLICT (code) DO NOTHING"),
            {"c": code, "s": seq, "l": label, "col": color})

    for code, minv, unit, note in ROAD_THRESHOLDS:
        # ⚠️ 같은 이름의 파라미터를 두 자리에 쓰면 PostgreSQL 이 타입을 서로
        #    다르게 추론해 거절한다(text 와 varchar). 형을 못박는다 —
        #    실제로 이것 때문에 마이그레이션이 중간에 멈췄다.
        conn.execute(sa.text(
            "INSERT INTO level_thresholds "
            "(domain, level_code, min_value, unit, source_note, updated_by) "
            "SELECT 'road', CAST(:c AS varchar), :v, :u, :n, 'system' "
            "WHERE NOT EXISTS (SELECT 1 FROM level_thresholds "
            "                  WHERE domain='road' "
            "                    AND level_code = CAST(:c AS varchar))"),
            {"c": code, "v": minv, "u": unit, "n": note})


def downgrade() -> None:
    conn = op.get_bind()
    # 구간을 먼저 지운다 — level_thresholds 가 risk_levels 를 가리킨다.
    conn.execute(sa.text("DELETE FROM level_thresholds WHERE domain='road'"))
    conn.execute(sa.text("DELETE FROM risk_levels WHERE kind='maintenance'"))
    op.drop_column("risk_levels", "kind")
