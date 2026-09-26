"""승인 게이트 · 설정 프로필 · HMAC 인증 (사양서 2026-09-26 개정, ADR-003/004, docs/03)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26

바뀌는 것:
  · config_profile 신설 + 시드 3행 (id 1~3, 시퀀스를 4 부터로 맞춘다)
  · device: mqtt_password_hash / ti_server / grp0 / grp1 삭제.
            register_ack_at, ka_device, profile_id(FK, 기본 1), ti_override, ka_override,
            address, bjd_code, grp, online_changed_at 추가.
            site 를 varchar(24) 로(기존 값은 24자로 자른다 — 단말 OLED 한계).
            state 기본값 ACTIVE → PENDING (기존 행은 그대로 둔다 — 2차에 이미 붙은
            단말을 갑자기 미승인으로 돌리면 Telemetry 가 끊긴다).
  · 인덱스: (last_seen_at), (grp), (profile_id) 추가. grp0/grp1 인덱스 삭제.
  · device_event.kind 는 자유 텍스트라 DDL 변경 없음. 새 값: REGISTER_ACK, ONLINE, OFFLINE,
    CONFIG_SET (docs/03).

ti_server 를 지우기 전에 프로필로 옮기지 않는다 — 2차 시험 단말은 전부 기본 600 이고
프로필 1(600/300)이 그 값이다. 600 이 아닌 단말이 있었다면 ti_override 로 보존한다.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")

#: docs/03 시드. MCU 사양서 §14.6 권장 프로필.
_PROFILE_SEED = (
    (1, "1,100원 시험", 600, 300),
    (2, "1,100원 운영", 1800, 600),
    (3, "2,200원 관제", 300, 120),
)


def upgrade() -> None:
    # ── config_profile ──────────────────────────────────────────────────
    op.create_table(
        "config_profile",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("ti", sa.Integer, nullable=False),
        sa.Column("ka", sa.Integer, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
        sa.CheckConstraint("ti BETWEEN 60 AND 3600", name="ck_profile_ti_range"),
        sa.CheckConstraint("ka BETWEEN 60 AND 1800", name="ck_profile_ka_range"),
    )
    for pid, name, ti, ka in _PROFILE_SEED:
        op.execute(
            sa.text("INSERT INTO config_profile (id, name, ti, ka) VALUES (:id, :name, :ti, :ka)")
            .bindparams(id=pid, name=name, ti=ti, ka=ka)
        )
    # 시드가 id 를 직접 넣었으므로 serial 시퀀스를 그 뒤로 옮긴다. 안 하면 첫 POST 가 id=1 충돌.
    op.execute(
        "SELECT setval(pg_get_serial_sequence('config_profile', 'id'), "
        "(SELECT max(id) FROM config_profile))"
    )

    # ── device: 추가 ────────────────────────────────────────────────────
    op.add_column("device", sa.Column("register_ack_at", _TS))
    op.add_column("device", sa.Column("ka_device", sa.Integer))
    op.add_column(
        "device",
        sa.Column("profile_id", sa.Integer, sa.ForeignKey("config_profile.id"),
                  nullable=False, server_default="1"),
    )
    op.add_column("device", sa.Column("ti_override", sa.Integer))
    op.add_column("device", sa.Column("ka_override", sa.Integer))
    op.add_column("device", sa.Column("address", sa.Text))
    op.add_column("device", sa.Column("bjd_code", sa.CHAR(10)))
    op.add_column("device", sa.Column("grp", sa.CHAR(12)))
    op.add_column("device", sa.Column("online_changed_at", _TS))

    # 2차에 ti_server 를 600 이 아닌 값으로 바꿔 둔 단말이 있으면 override 로 보존한다.
    op.execute("UPDATE device SET ti_override = ti_server WHERE ti_server <> 600")

    # ── device: 삭제 ────────────────────────────────────────────────────
    op.drop_constraint("ck_device_ti_server_range", "device", type_="check")
    op.drop_index("ix_device_grp0", table_name="device")
    op.drop_index("ix_device_grp1", table_name="device")
    op.drop_column("device", "mqtt_password_hash")
    op.drop_column("device", "ti_server")
    op.drop_column("device", "grp0")
    op.drop_column("device", "grp1")

    # ── device: 변경 ────────────────────────────────────────────────────
    op.execute("UPDATE device SET site = left(site, 24) WHERE length(site) > 24")
    op.alter_column("device", "site", type_=sa.String(24), existing_type=sa.Text)
    op.alter_column("device", "state", server_default="PENDING", existing_type=sa.Text,
                    existing_nullable=False)
    op.create_check_constraint(
        "ck_device_ti_override_range", "device",
        "ti_override IS NULL OR ti_override BETWEEN 60 AND 3600",
    )
    op.create_check_constraint(
        "ck_device_ka_override_range", "device",
        "ka_override IS NULL OR ka_override BETWEEN 60 AND 1800",
    )
    op.create_index("ix_device_last_seen_at", "device", ["last_seen_at"])
    op.create_index("ix_device_grp", "device", ["grp"])
    op.create_index("ix_device_profile_id", "device", ["profile_id"])


def downgrade() -> None:
    op.drop_index("ix_device_profile_id", table_name="device")
    op.drop_index("ix_device_grp", table_name="device")
    op.drop_index("ix_device_last_seen_at", table_name="device")
    op.drop_constraint("ck_device_ka_override_range", "device", type_="check")
    op.drop_constraint("ck_device_ti_override_range", "device", type_="check")
    op.alter_column("device", "state", server_default="ACTIVE", existing_type=sa.Text,
                    existing_nullable=False)
    op.alter_column("device", "site", type_=sa.Text, existing_type=sa.String(24))

    op.add_column("device", sa.Column("grp1", sa.Text))
    op.add_column("device", sa.Column("grp0", sa.Text))
    op.add_column("device", sa.Column("ti_server", sa.Integer, nullable=False,
                                      server_default="600"))
    op.add_column("device", sa.Column("mqtt_password_hash", sa.Text))
    op.execute("UPDATE device SET ti_server = ti_override WHERE ti_override IS NOT NULL")
    op.create_check_constraint("ck_device_ti_server_range", "device",
                               "ti_server BETWEEN 60 AND 3600")
    op.create_index("ix_device_grp0", "device", ["grp0"])
    op.create_index("ix_device_grp1", "device", ["grp1"])

    for col in ("online_changed_at", "grp", "bjd_code", "address", "ka_override",
                "ti_override", "profile_id", "ka_device", "register_ack_at"):
        op.drop_column("device", col)
    op.drop_table("config_profile")
