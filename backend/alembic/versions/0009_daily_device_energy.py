"""일 집계에 단말이 적산한 발전량·사용량 (문제점 23번, 2026-10-01)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01

단말 빌드 2026-10-01-1 부터 Telemetry 에 eg·eu(금일)·yg·yu(전일) kWh×100 이 온다(사양서 §1.1.6).
그날 값 = 다음날 00:05 뒤 첫 yg·yu, 0 이면 그날 마지막 eg·eu → telemetry_daily.gen_wh / use_wh (Wh).
옛 펌웨어·MPPT 무응답은 NULL. 기존 pp_wh·li_ah(서버 적분)는 그대로 둔다.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telemetry_daily", sa.Column("gen_wh", sa.Integer()))
    op.add_column("telemetry_daily", sa.Column("use_wh", sa.Integer()))


def downgrade() -> None:
    op.drop_column("telemetry_daily", "use_wh")
    op.drop_column("telemetry_daily", "gen_wh")
