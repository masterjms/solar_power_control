"""5차 — 법정동 트리 · COMMAND 대상 스냅숏 · override 표시 (ADR-005, docs/03 "5차 추가")

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27

바뀌는 것:
  · region 신설 — 시도 > 시군구 > 법정동(말단). 말단만 bjd_code(10자리, UNIQUE).
    같은 부모 아래 이름 UNIQUE(최상위는 부분 인덱스로 따로).
  · device: node_id(FK region, 말단), override_act / override_level / override_seq /
    override_until 추가. grp·bjd_code 는 0002 에 이미 있다(배정 때 같이 채운다).
  · command: target_kind CHECK 를 device/node/all 로(2차 코드는 PING 개별만 만들었으므로
    'group' 행은 없다). created_by, topics(jsonb), exp 추가. 미종료 부분 인덱스(종료 타이머용).
  · command_target 신설 — (seq, uuid) PK, status CHECK, (uuid, status) 인덱스(자동 재시도 조회).
  · device_event.kind 는 자유 텍스트라 DDL 없음. 새 값 COMMAND_SENT, COMMAND_ACK.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")
_UUID = postgresql.CHAR(24)


def upgrade() -> None:
    # ── region ──────────────────────────────────────────────────────────
    op.create_table(
        "region",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("region.id")),
        sa.Column("level", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("bjd_code", sa.CHAR(10), unique=True),
        sa.Column("lat", sa.Float),
        sa.Column("lon", sa.Float),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.CheckConstraint("level IN ('sido','sigungu','dong')", name="ck_region_level"),
        sa.CheckConstraint("(level = 'dong') = (bjd_code IS NOT NULL)",
                           name="ck_region_leaf_code"),
        sa.CheckConstraint("bjd_code IS NULL OR bjd_code ~ '^[0-9]{10}$'",
                           name="ck_region_bjd_code_format"),
    )
    op.create_index("ix_region_parent_id", "region", ["parent_id"])
    op.create_index("ux_region_parent_name", "region", ["parent_id", "name"], unique=True,
                    postgresql_where=sa.text("parent_id IS NOT NULL"))
    op.create_index("ux_region_root_name", "region", ["name"], unique=True,
                    postgresql_where=sa.text("parent_id IS NULL"))

    # ── device ──────────────────────────────────────────────────────────
    op.add_column("device", sa.Column("node_id", sa.Integer, sa.ForeignKey("region.id")))
    op.add_column("device", sa.Column("override_act", sa.Text))
    op.add_column("device", sa.Column("override_level", sa.Text))
    op.add_column("device", sa.Column("override_seq", sa.BigInteger))
    op.add_column("device", sa.Column("override_until", _TS))
    op.create_index("ix_device_node_id", "device", ["node_id"])

    # ── command ─────────────────────────────────────────────────────────
    op.drop_constraint("ck_command_target_kind", "command", type_="check")
    op.create_check_constraint(
        "ck_command_target_kind", "command", "target_kind IN ('device','node','all')"
    )
    op.add_column("command", sa.Column("created_by", sa.Text))
    op.add_column("command", sa.Column("topics", postgresql.JSONB))
    op.add_column("command", sa.Column("exp", sa.Integer))
    op.create_index("ix_command_unfinished", "command", ["seq"],
                    postgresql_where=sa.text("finished_at IS NULL"))

    # ── command_target ──────────────────────────────────────────────────
    op.create_table(
        "command_target",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("command.seq", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("uuid", _UUID, primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_sent_at", _TS),
        sa.Column("acked_at", _TS),
        sa.Column("ack", postgresql.JSONB),
        sa.CheckConstraint(
            "status IN ('pending','OK','LOCAL','EXPIRED','BAD','STATE')",
            name="ck_command_target_status",
        ),
    )
    op.create_index("ix_command_target_uuid_status", "command_target", ["uuid", "status"])


def downgrade() -> None:
    op.drop_index("ix_command_target_uuid_status", table_name="command_target")
    op.drop_table("command_target")

    op.drop_index("ix_command_unfinished", table_name="command")
    op.drop_column("command", "exp")
    op.drop_column("command", "topics")
    op.drop_column("command", "created_by")
    op.drop_constraint("ck_command_target_kind", "command", type_="check")
    # node 행은 되돌릴 곳이 없다 — 지운다(5차 이력은 downgrade 와 함께 사라진다).
    op.execute("DELETE FROM command WHERE target_kind = 'node'")
    op.create_check_constraint(
        "ck_command_target_kind", "command", "target_kind IN ('device','group','all')"
    )

    op.drop_index("ix_device_node_id", table_name="device")
    for col in ("override_until", "override_seq", "override_level", "override_act", "node_id"):
        op.drop_column("device", col)

    op.drop_index("ux_region_root_name", table_name="region")
    op.drop_index("ux_region_parent_name", table_name="region")
    op.drop_index("ix_region_parent_id", table_name="region")
    op.drop_table("region")
