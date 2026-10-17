"""비밀번호 변경 강제 표시 (문제점 54번, 2026-10-17)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-17

admin_user.must_change_password — 최고관리자가 정한 비밀번호(새 계정·재설정)는 본인이 로그인 뒤 바꾸게 한다.
연속 실패 잠금은 login_log 로 판단하므로 새 칸이 필요 없다.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_user", sa.Column("must_change_password", sa.Boolean(), nullable=False,
                                          server_default="false"))
    op.create_index("ix_login_log_username_at", "login_log", ["username", "at"])


def downgrade() -> None:
    op.drop_index("ix_login_log_username_at", table_name="login_log")
    op.drop_column("admin_user", "must_change_password")
