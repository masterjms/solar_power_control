"""서버 설정 표 (문제점 14번, 2026-10-01)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01

운영 중 화면에서 최고관리자가 바꾸는 값(원격 명령 응답 대기 시간·보내는 횟수 …)을 둔다.
항목 정의·범위·기본값은 app/core/server_settings.ITEMS. 행이 없으면 기본값이라 채워 넣지 않는다.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("server_setting")
