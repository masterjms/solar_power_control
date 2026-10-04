"""계정 3단계(최고관리자·지역관리자·게스트)와 로그인 기록 (문제점 21번, 2026-10-04, ADR-013)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-04

admin_user(3차에 만들고 안 쓰던 표)를 넓힌다: 역할·맡은 시·도·만료·사용 중지·만든 사람·마지막 로그인·
비밀번호 바꾼 시각. 옛 행은 없다(쓴 적 없음). login_log 새로. schedule_profile.created_by 추가.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("admin_user", "role", server_default="guest")
    op.add_column("admin_user", sa.Column("region_ids", postgresql.ARRAY(sa.Integer())))
    op.add_column("admin_user", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("admin_user", sa.Column("disabled", sa.Boolean(), nullable=False,
                                          server_default="false"))
    op.add_column("admin_user", sa.Column("created_by", sa.Text()))
    op.add_column("admin_user", sa.Column("last_login_at", sa.DateTime(timezone=True)))
    op.add_column("admin_user", sa.Column("password_changed_at", sa.DateTime(timezone=True),
                                          nullable=False, server_default=sa.func.now()))
    op.create_check_constraint("ck_admin_user_role", "admin_user",
                               "role IN ('super_admin','region_admin','guest','admin')")
    op.create_table(
        "login_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ip", sa.Text()),
    )
    op.create_index("ix_login_log_at", "login_log", ["at"])
    op.add_column("schedule_profile", sa.Column("created_by", sa.Text()))


def downgrade() -> None:
    op.drop_column("schedule_profile", "created_by")
    op.drop_index("ix_login_log_at", table_name="login_log")
    op.drop_table("login_log")
    op.drop_constraint("ck_admin_user_role", "admin_user", type_="check")
    for c in ("password_changed_at", "last_login_at", "created_by", "disabled", "expires_at",
              "region_ids"):
        op.drop_column("admin_user", c)
    op.alter_column("admin_user", "role", server_default="admin")
