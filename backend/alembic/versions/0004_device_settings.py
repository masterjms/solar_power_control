"""S-23 단말 운전 설정 — device_settings · device_settings_history (ADR-007, docs/03 "단말 설정")

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27

바뀌는 것:
  · device_settings 신설 — 단말당 1행(uuid PK, FK device ON DELETE CASCADE). 25개 항목 int NULL
    (컬럼 이름 = ui_items.json key, 단말 정수 그대로), 표 조건 tbl_*, dip/bat, sh_device, ss_known,
    read_at, sync(CHECK 5종, 기본 unknown), last_report(jsonb), pending_*(재발송), last_result*.
  · device_settings_history 신설 — (uuid, changed_at) 인덱스. FK 없음(단말을 지워도 이력은 남긴다).
  · command.type 은 CHECK 가 없는 자유 텍스트라 DDL 없음. 새 값 SETTINGS_GET, SETTINGS_SET
    (command_target 은 만들지 않는다 — 상태는 device_settings 에).
  · device_event.kind 도 자유 텍스트. 새 값 SETTINGS_SENT, SETTINGS, SETTINGS_ACK.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")
_UUID = postgresql.CHAR(24)

#: ui_items.json 순서(이 파일은 그 시점의 스냅숏이다 — 항목이 늘면 새 리비전으로 더한다).
_KEYS = (
    "start_ofst", "stop_ofst", "start_pwm",
    "manual_40w", "manual_5w1", "manual_5w2", "fade",
    "stage1_h", "stage1_m", "stage1_pwm", "stage2_h", "stage2_m", "stage2_pwm",
    "stage3_h", "stage3_m", "stage3_pwm", "stage4_h", "stage4_m", "stage4_pwm",
    "cut12", "rtn12", "cut24", "rtn24", "cut_time", "rtn_time",
)


def upgrade() -> None:
    op.create_table(
        "device_settings",
        sa.Column("uuid", _UUID, sa.ForeignKey("device.uuid", ondelete="CASCADE"),
                  primary_key=True),
        *[sa.Column(k, sa.Integer) for k in _KEYS],
        sa.Column("tbl_region", sa.Text),
        sa.Column("tbl_lat_e6", sa.Integer),
        sa.Column("tbl_lon_e6", sa.Integer),
        sa.Column("tbl_on", sa.Integer),
        sa.Column("tbl_off", sa.Integer),
        sa.Column("tbl_src", sa.SmallInteger),
        sa.Column("tbl_crc", sa.CHAR(8)),
        sa.Column("dip", sa.SmallInteger),
        sa.Column("bat", sa.SmallInteger),
        sa.Column("sh_device", sa.CHAR(8)),
        sa.Column("ss_known", sa.Integer),
        sa.Column("read_at", _TS),
        sa.Column("sync", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("last_report", postgresql.JSONB),
        sa.Column("pending_seq", sa.BigInteger),
        sa.Column("pending_kind", sa.Text),
        sa.Column("pending_body", postgresql.JSONB),
        sa.Column("pending_sent_at", _TS),
        sa.Column("pending_attempts", sa.Integer),
        sa.Column("last_result", sa.Text),
        sa.Column("last_result_at", _TS),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
        sa.CheckConstraint(
            "sync IN ('unknown','synced','writing','local_saved','device_changed')",
            name="ck_device_settings_sync",
        ),
        sa.CheckConstraint(
            "pending_kind IS NULL OR pending_kind IN ('SETTINGS_GET','SETTINGS_SET')",
            name="ck_device_settings_pending_kind",
        ),
    )

    op.create_table(
        "device_settings_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("uuid", _UUID, nullable=False),
        sa.Column("changed_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("by", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("old", sa.Integer),
        sa.Column("new", sa.Integer),
        sa.Column("note", sa.Text),
    )
    op.create_index("ix_device_settings_history_uuid_changed_at", "device_settings_history",
                    ["uuid", "changed_at"])


def downgrade() -> None:
    op.drop_index("ix_device_settings_history_uuid_changed_at",
                  table_name="device_settings_history")
    op.drop_table("device_settings_history")
    op.drop_table("device_settings")
