"""단말별 누적 발전·사용 (문제점 27·29번, 2026-10-03)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-03

`device.energy_gen_wh_total` / `energy_use_wh_total`(Wh): 일 집계(00:30)가 `telemetry_daily` 를 통계 시작일부터
다시 더해 적는다(멱등). 대시보드 "누적 발전·감축"은 이 값 + 오늘. 원문·하루 요약을 지워도 누적은 남는다.
"통계 다시 시작"(POST /api/energy/reset)은 0 으로 되돌린다.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("device", sa.Column("energy_gen_wh_total", sa.BigInteger(), nullable=False,
                                      server_default="0"))
    op.add_column("device", sa.Column("energy_use_wh_total", sa.BigInteger(), nullable=False,
                                      server_default="0"))


def downgrade() -> None:
    op.drop_column("device", "energy_use_wh_total")
    op.drop_column("device", "energy_gen_wh_total")
