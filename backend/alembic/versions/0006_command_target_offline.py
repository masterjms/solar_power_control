"""명령 대상 상태에 OFFLINE·NO_RESPONSE 추가 (문제점 14번, 2026-09-29)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

그룹 명령에 오프라인 단말이 섞이면 그 단말의 응답을 COMMAND_TIMEOUT_SEC(900초) 동안 기다렸다.
  · OFFLINE     — 보낼 때 오프라인. 보내지 않고(attempts 0) 기다리지도, 자동 재시도하지도 않는다.
  · NO_RESPONSE — 보냈지만 COMMAND_TIMEOUT_SEC 안에 응답이 없어 서버가 실패로 닫았다.
둘 다 뒤늦게 COMMAND_ACK 가 오면 그 result 로 바뀐다. 기존 행은 건드리지 않는다.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_OLD = "status IN ('pending','OK','LOCAL','EXPIRED','BAD','STATE')"
_NEW = "status IN ('pending','OK','LOCAL','EXPIRED','BAD','STATE','OFFLINE','NO_RESPONSE')"


def upgrade() -> None:
    op.drop_constraint("ck_command_target_status", "command_target", type_="check")
    op.create_check_constraint("ck_command_target_status", "command_target", _NEW)


def downgrade() -> None:
    # 새 상태는 옛 제약에 없다 — 무응답으로 되돌려 둔다(옛 의미: 응답 없음 = pending).
    op.execute("UPDATE command_target SET status = 'pending' "
               "WHERE status IN ('OFFLINE','NO_RESPONSE')")
    op.drop_constraint("ck_command_target_status", "command_target", type_="check")
    op.create_check_constraint("ck_command_target_status", "command_target", _OLD)
