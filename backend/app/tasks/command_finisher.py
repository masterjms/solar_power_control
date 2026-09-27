"""COMMAND 종료 판정 — 30초마다 (ADR-005 "명령 종료").

미종료 COMMAND 마다 대상 상태를 세어 core/command_rules.finish_result 로 판정하고
`finished_at`·`result` 를 채운다.
  · 모든 대상이 종결(OK/LOCAL/BAD/STATE, 또는 시도를 다 쓴 EXPIRED) → OK(전부 OK) / PARTIAL
  · COMMAND_TIMEOUT_SEC(900) 경과 → 응답 0 이면 TIMEOUT, 아니면 PARTIAL
끝난 명령은 자동 재시도도 멈춘다(재시도 조건에 "미종료"가 있다).

같은 판정을 COMMAND_ACK 수신 직후에도 그 seq 하나에 대해 돌린다(finish_if_done) — 개별 명령이
OK 를 받고 30초 동안 "진행 중"으로 보이지 않게.

타이머 끝에 재시도 후보 uuid 집합(CommandRetrier)을 DB 기준으로 다시 맞춘다.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import MsgType, TargetStatus
from app.core.command_rules import finish_result
from app.db import session_scope
from app.models.command import Command, CommandTarget

log = logging.getLogger(__name__)


async def finish_due(
    db: AsyncSession, now: dt.datetime, *, seqs: list[int] | None = None
) -> dict[int, str]:
    """판정해서 끝난 명령을 닫는다. {seq: result}."""
    t = CommandTarget
    counts_q = (
        select(
            Command.seq, Command.sent_at,
            func.count(t.uuid),
            *[func.count(t.uuid).filter(t.status == s.value) for s in TargetStatus],
            func.count(t.uuid).filter(and_(
                t.status == TargetStatus.EXPIRED.value,
                t.attempts >= settings.command_max_attempts,
            )),
        )
        .select_from(Command)
        .join(t, t.seq == Command.seq, isouter=True)
        .where(Command.finished_at.is_(None), Command.type == MsgType.COMMAND.value)
        .group_by(Command.seq, Command.sent_at)
    )
    if seqs is not None:
        counts_q = counts_q.where(Command.seq.in_(seqs))
    done: dict[int, str] = {}
    statuses = [s.value for s in TargetStatus]
    for row in (await db.execute(counts_q)).all():
        seq, sent_at, total = row[0], row[1], int(row[2])
        counts = {s: int(n) for s, n in zip(statuses, row[3:3 + len(statuses)], strict=True)}
        exhausted = int(row[3 + len(statuses)])
        result = finish_result(
            counts=counts, expired_exhausted=exhausted, total=total,
            elapsed_sec=(now - sent_at).total_seconds(), timeout_sec=settings.command_timeout_sec,
        )
        if result is None:
            continue
        await db.execute(
            update(Command)
            .where(Command.seq == seq, Command.finished_at.is_(None))
            .values(finished_at=now, result=result)
        )
        done[int(seq)] = result
    return done


async def run(retrier=None) -> None:
    """스케줄러 진입점. 실패해도 다음 주기에 다시 한다."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        async with session_scope() as db:
            done = await finish_due(db, now)
            if retrier is not None:
                await retrier.rebuild(db)
    except Exception:  # noqa: BLE001
        log.exception("COMMAND 종료 판정 실패 (30초 뒤 재시도)")
        return
    for seq, result in done.items():
        log.info("COMMAND seq=%d 종료: %s", seq, result)
