"""ID 발번.

seq 는 반드시 DB 시퀀스로 뽑는다(사양서 §1.1.5 "서버 전역 단조 증가", CLAUDE.md 원칙).
프로세스 메모리 카운터를 쓰면 재기동 때 되감기고, 단말이 이미 본 seq 가 다시 나와서
중복 실행 방지가 깨진다. 사양서 §4.2 의 `server_state.last_seq` 를 시퀀스로 대체했다.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import SEQ_MAX

#: 마이그레이션 0001 에서 만든다. MAXVALUE 4294967295 (uint32).
CMD_SEQUENCE = "cmd_seq"

#: 이 값을 넘으면 /api/metrics 에 경보 플래그를 세운다. 42억의 99%.
SEQ_ALERT_THRESHOLD = int(SEQ_MAX * 0.99)


async def next_cmd_seq(db: AsyncSession) -> int:
    """PING / CMD / SCH / OTA 가 공유하는 단일 seq 공간. topic 과 무관하게 유일하다."""
    value = await db.scalar(text(f"SELECT nextval('{CMD_SEQUENCE}')"))
    return int(value)


async def current_cmd_seq(db: AsyncSession) -> int | None:
    """마지막으로 발번된 값. 아직 한 번도 안 뽑았으면 None."""
    row = (await db.execute(text(f"SELECT last_value, is_called FROM {CMD_SEQUENCE}"))).first()
    if row is None or not row[1]:
        return None
    return int(row[0])
