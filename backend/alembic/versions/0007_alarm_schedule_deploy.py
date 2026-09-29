"""알람(S-24) + 스케줄 배포(S-25) — alarm, schedule_profile, schedule_assign, deploy_job, deploy_item,
device_schedule (ADR-009, ADR-010, docs/03)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29

바뀌는 것:
  · alarm — 한 단말·한 항목 열린 알람 1건(부분 유니크). 관찰 행(opened_at NULL) → 열림 → 해제(closed_at).
  · schedule_profile — 표 조건 + crc + 운전 15개(jsonb), version.
  · schedule_assign — 노드 또는 단말 하나 → 프로필(둘 중 하나만, CHECK).
  · deploy_job / deploy_item — 배포 한 번과 단말별 진행(waiting/reading/sent → 결과).
  · device_schedule — 단말에 적용된(ACK OK) 프로필 판·crc.
기존 테이블: device_event 에 인덱스 (kind, received_at) 하나 — 알람 "재부팅 잦음" 집계용.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")
_UUID = postgresql.CHAR(24)

_ITEM_STATUSES = (
    "waiting", "reading", "sent",
    "OK", "CRC", "RULE", "RANGE", "BAD", "STATE", "FLASH", "NO_RESPONSE", "READ_FAILED",
    "CANCELLED", "SUPERSEDED",
)


def upgrade() -> None:
    op.create_table(
        "alarm",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("uuid", _UUID, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("first_seen_at", _TS, nullable=False),
        sa.Column("opened_at", _TS),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("closed_at", _TS),
        sa.Column("value", postgresql.JSONB),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_index("ux_alarm_open", "alarm", ["uuid", "kind"], unique=True,
                    postgresql_where=sa.text("closed_at IS NULL"))
    op.create_index("ix_alarm_closed_at", "alarm", ["closed_at"])
    op.create_index("ix_alarm_uuid", "alarm", ["uuid"])
    # "오늘 재부팅 n회" 집계(재조정 30초마다) — device_event 는 1년치라 kind·시각 인덱스가 필요하다.
    op.create_index("ix_device_event_kind_received_at", "device_event", ["kind", "received_at"])

    op.create_table(
        "schedule_profile",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("region", sa.Text, nullable=False),
        sa.Column("lat_e6", sa.Integer, nullable=False),
        sa.Column("lon_e6", sa.Integer, nullable=False),
        sa.Column("on_corr", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("off_corr", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("crc", postgresql.CHAR(8), nullable=False),
        sa.Column("values", postgresql.JSONB, nullable=False),
        sa.Column("address", sa.Text),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("updated_by", sa.Text),
    )

    op.create_table(
        "schedule_assign",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("node_id", sa.Integer, sa.ForeignKey("region.id", ondelete="CASCADE"),
                  unique=True),
        sa.Column("uuid", _UUID, sa.ForeignKey("device.uuid", ondelete="CASCADE"), unique=True),
        sa.Column("profile_id", sa.Integer,
                  sa.ForeignKey("schedule_profile.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assigned_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("assigned_by", sa.Text),
        sa.CheckConstraint("(node_id IS NULL) <> (uuid IS NULL)",
                           name="ck_schedule_assign_target"),
    )
    op.create_index("ix_schedule_assign_profile_id", "schedule_assign", ["profile_id"])

    op.create_table(
        "deploy_job",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("profile_id", sa.Integer,
                  sa.ForeignKey("schedule_profile.id", ondelete="SET NULL")),
        sa.Column("profile_name", sa.Text, nullable=False),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column("crc", postgresql.CHAR(8), nullable=False),
        sa.Column("spec", postgresql.JSONB, nullable=False),
        sa.Column("scope_kind", sa.Text, nullable=False),
        sa.Column("scope_id", sa.Text),
        sa.Column("scope_label", sa.Text),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.Text),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("finished_at", _TS),
        sa.Column("cancelled_at", _TS),
    )
    op.create_index("ix_deploy_job_profile_id", "deploy_job", ["profile_id"])

    op.create_table(
        "deploy_item",
        sa.Column("job_id", sa.Integer, sa.ForeignKey("deploy_job.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("uuid", _UUID, primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default="waiting"),
        sa.Column("first_seq", sa.BigInteger),
        sa.Column("rounds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sent_at", _TS),
        sa.Column("acked_at", _TS),
        sa.Column("detail", sa.Text),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
        sa.CheckConstraint("status IN (" + ",".join(f"'{s}'" for s in _ITEM_STATUSES) + ")",
                           name="ck_deploy_item_status"),
    )
    op.create_index("ix_deploy_item_open", "deploy_item", ["uuid"],
                    postgresql_where=sa.text("status IN ('waiting','reading','sent')"))

    op.create_table(
        "device_schedule",
        sa.Column("uuid", _UUID, sa.ForeignKey("device.uuid", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("profile_id", sa.Integer,
                  sa.ForeignKey("schedule_profile.id", ondelete="SET NULL")),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column("crc", postgresql.CHAR(8), nullable=False),
        sa.Column("values", postgresql.JSONB, nullable=False),
        sa.Column("job_id", sa.Integer),
        sa.Column("applied_at", _TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_index("ix_device_event_kind_received_at", table_name="device_event")
    op.drop_table("device_schedule")
    op.drop_table("deploy_item")
    op.drop_table("deploy_job")
    op.drop_table("schedule_assign")
    op.drop_table("schedule_profile")
    op.drop_table("alarm")
