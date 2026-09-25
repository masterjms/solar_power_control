"""초기 스키마 — docs/03_DB_스키마.md 그대로 (7 테이블 + cmd_seq 시퀀스 + 월 파티션 2개)

Revision ID: 0001
Revises:
Create Date: 2026-09-25

autogenerate 가 아니라 손으로 썼다. telemetry 는 PARTITION BY RANGE 라 op.create_table 로
못 만들고, 파티션 생성 DDL 은 app/tasks/partitions.py 와 같은 함수를 써서 이름·경계가
운영 중 생성분과 어긋나지 않게 한다.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.tasks.partitions import partition_ddl, partition_name, wanted_partitions

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")
_UUID = postgresql.CHAR(24)


def upgrade() -> None:
    # 사양서 §1.1.5 전역 seq. uint32 상한에서 멈춘다(CYCLE 없음) — 42억 개를 다 쓰기 전에
    # /api/metrics 의 cmd_seq 경보로 알아채는 것이 맞다.
    op.execute("CREATE SEQUENCE IF NOT EXISTS cmd_seq AS bigint START 1 MAXVALUE 4294967295")

    # ── device ──────────────────────────────────────────────────────────
    op.create_table(
        "device",
        sa.Column("uuid", _UUID, primary_key=True),
        sa.Column("state", sa.Text, nullable=False, server_default="ACTIVE"),
        sa.Column("state_reason", sa.Text),
        sa.Column("state_changed_at", _TS),
        sa.Column("mqtt_password_hash", sa.Text),
        sa.Column("fw", sa.Text),
        sa.Column("device_model", sa.Text),
        sa.Column("modem_model", sa.Text),
        sa.Column("imei", sa.Text),
        sa.Column("iccid", sa.Text),
        sa.Column("msisdn", sa.Text),
        sa.Column("cv_device", sa.Integer),
        sa.Column("ss_device", sa.Integer),
        sa.Column("ti_device", sa.Integer),
        sa.Column("cv_server", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ti_server", sa.Integer, nullable=False, server_default="600"),
        sa.Column("lat", sa.Float),
        sa.Column("lon", sa.Float),
        sa.Column("site", sa.Text),
        sa.Column("grp0", sa.Text),
        sa.Column("grp1", sa.Text),
        sa.Column("last_register_at", _TS),
        sa.Column("last_telemetry_at", _TS),
        sa.Column("last_seen_at", _TS),
        sa.Column("last_sq", sa.BigInteger),
        sa.Column("last_telemetry", postgresql.JSONB),
        sa.Column("online", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("offline_at", _TS),
        sa.Column("lost_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reboot_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("config_sent_at", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
        sa.CheckConstraint("uuid ~ '^[0-9A-F]{24}$'", name="ck_device_uuid_format"),
        sa.CheckConstraint("ti_server BETWEEN 60 AND 3600", name="ck_device_ti_server_range"),
        sa.CheckConstraint(
            "state IN ('PENDING','ACTIVE','SUSPENDED','REJECTED','RETIRED')",
            name="ck_device_state",
        ),
    )
    op.create_index("ix_device_state", "device", ["state"])
    op.create_index("ix_device_last_telemetry_at", "device", ["last_telemetry_at"])
    op.create_index("ix_device_grp0", "device", ["grp0"])
    op.create_index("ix_device_grp1", "device", ["grp1"])

    # ── telemetry (월 파티션) ────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE telemetry (
          uuid         char(24)    NOT NULL,
          received_at  timestamptz NOT NULL,
          ts_device    text,
          sq           bigint,
          fw           text, ss int, cv int,
          er int, "on" smallint, md smallint,
          pw1 smallint, pw2 smallint, pw3 smallint,
          bv int, bi int, sc smallint, pp int, li int, cs int,
          raw          jsonb       NOT NULL,
          PRIMARY KEY (uuid, received_at)
        ) PARTITION BY RANGE (received_at)
        """
    )
    op.execute("CREATE INDEX ix_telemetry_received_at ON telemetry (received_at)")
    for year, month in wanted_partitions(dt.datetime.now(dt.timezone.utc)):
        op.execute(partition_ddl(year, month))

    # ── telemetry_daily ─────────────────────────────────────────────────
    op.create_table(
        "telemetry_daily",
        sa.Column("uuid", _UUID, primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("samples", sa.Integer, nullable=False),
        sa.Column("pp_wh", sa.Float),
        sa.Column("li_ah", sa.Float),
        sa.Column("bv_min", sa.Integer),
        sa.Column("bv_max", sa.Integer),
        sa.Column("sc_min", sa.SmallInteger),
        sa.Column("sc_max", sa.SmallInteger),
        sa.Column("on_minutes", sa.Integer),
    )

    # ── device_event ────────────────────────────────────────────────────
    op.create_table(
        "device_event",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("uuid", _UUID, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("dedup_key", sa.Text),
        sa.Column("received_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_device_event_uuid_received_at", "device_event", ["uuid", "received_at"])
    op.create_index("ix_device_event_received_at", "device_event", ["received_at"])
    op.create_index(
        "ux_device_event_dedup_key",
        "device_event",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL"),
    )

    # ── command · command_ack ───────────────────────────────────────────
    op.create_table(
        "command",
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("target_kind", sa.Text, nullable=False),
        sa.Column("target_id", sa.Text),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("expected_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("acked_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sent_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("finished_at", _TS),
        sa.Column("result", sa.Text),
        sa.CheckConstraint("seq BETWEEN 0 AND 4294967295", name="ck_command_seq_uint32"),
        sa.CheckConstraint(
            "target_kind IN ('device','group','all')", name="ck_command_target_kind"
        ),
    )
    op.create_index("ix_command_target_id", "command", ["target_id"])

    op.create_table(
        "command_ack",
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("uuid", _UUID, primary_key=True),
        sa.Column("result", sa.Text),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("received_at", _TS, nullable=False, server_default=_NOW),
    )

    # ── mqtt_account_export (단일 행) ───────────────────────────────────
    op.create_table(
        "mqtt_account_export",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("passwd_md5", sa.Text),
        sa.Column("acl_md5", sa.Text),
        sa.Column("acl_applied_md5", sa.Text),
        sa.Column("exported_at", _TS),
        sa.Column("applied_at", _TS),
        sa.CheckConstraint("id = 1", name="ck_mqtt_account_export_singleton"),
    )
    op.execute("INSERT INTO mqtt_account_export (id) VALUES (1)")

    # ── admin_user (3차) ────────────────────────────────────────────────
    op.create_table(
        "admin_user",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="admin"),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )


def downgrade() -> None:
    op.drop_table("admin_user")
    op.drop_table("mqtt_account_export")
    op.drop_table("command_ack")
    op.drop_table("command")
    op.drop_table("device_event")
    op.drop_table("telemetry_daily")
    for year, month in wanted_partitions(dt.datetime.now(dt.timezone.utc)):
        op.execute(f"DROP TABLE IF EXISTS {partition_name(year, month)}")
    op.execute("DROP TABLE IF EXISTS telemetry CASCADE")
    op.drop_table("device")
    op.execute("DROP SEQUENCE IF EXISTS cmd_seq")
