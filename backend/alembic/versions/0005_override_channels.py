"""원격 표시를 채널별로 — device.override_ch (F/W 2026-09-27-9, 사양서 §3.10.8 개정)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28

단말은 개별 > 그룹 > 전체 계층 슬롯을 버리고 **채널마다 마지막에 받은 명령 하나**만 둔다.
경로와 무관하게 나중 명령이 이기고, 다른 채널은 서로 건드리지 않는다(주등 on 뒤 입간판 off).

바뀌는 것:
  · device.override_ch jsonb NULL — {"1": {"act","seq","until","level"}, "2": {...}}
  · 기존 override_act/level/seq/until 은 **요약**으로 남긴다(가장 늦게 끝나는 채널). 목록 필터
    (remote=true)·기존 화면이 그대로 쓴다.
  · 백필: override_seq 가 있는 단말은 그 명령의 payload.ch 채널마다 같은 값을 넣는다(ch 가 없으면 1·2·3).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("device", sa.Column("override_ch", postgresql.JSONB(), nullable=True))
    op.execute(
        """
        UPDATE device d SET override_ch = sub.m
        FROM (
          SELECT d2.uuid,
                 jsonb_object_agg(ch.v, jsonb_build_object(
                   'act', d2.override_act, 'level', d2.override_level,
                   'seq', d2.override_seq, 'until', to_jsonb(d2.override_until))) AS m
          FROM device d2
          JOIN command c ON c.seq = d2.override_seq
          CROSS JOIN LATERAL jsonb_array_elements_text(
            coalesce(c.payload->'ch', '[1,2,3]'::jsonb)) AS ch(v)
          WHERE d2.override_seq IS NOT NULL AND d2.override_until IS NOT NULL
          GROUP BY d2.uuid
        ) sub
        WHERE d.uuid = sub.uuid
        """
    )


def downgrade() -> None:
    op.drop_column("device", "override_ch")
