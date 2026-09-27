"""COMMAND 개별 재시도 — "그 단말이 뭔가 보낸 직후" (ADR-005, 사양서 §1.1.10 · §3.10.11 S-17/S-18).

서버 → 단말 발행은 즉시 나가지만 모뎀이 도착을 수십 초~5분 늦게 넘겨준다. exp 30초와 겹치면
EXPIRED 가 나고, 연결이 잠깐 끊겨 있었으면 아예 무응답(pending)이다. 그 단말이 REGISTER/
TELEMETRY 를 보낸 **직후**는 모뎀이 깨어 있어 바로 받을 확률이 가장 높은 순간이라 그때
개별 topic 으로 같은 seq·새 ts 로 다시 보낸다(그룹 재발행 금지 — 이미 받은 단말까지 다시 처리).

싸야 한다: 1만 대가 10분마다 TM 을 보내므로 flush 마다 이 경로를 탄다.
  1. 메모리 집합 `_pending`(재시도 후보 대상이 있는 uuid)에 없으면 **쿼리 자체를 안 한다**.
     기동 시 DB 에서 채우고(warm), 명령 발송 때 넣고(mark), 종료 타이머(30초)가 DB 로 다시 맞춘다
     (rebuild — ACK 로 끝난 uuid 가 여기서 빠진다). 틀려도 한 번 더 조회할 뿐이다.
  2. 후보가 있으면 UPDATE … RETURNING 한 문장으로 **선점**한다(attempts+1, last_sent_at=now).
     같은 단말의 REGISTER 와 TM 이 연달아 와도 두 번째는 RETRY_MIN 조건에 걸려 안 나간다.
  3. 선점한 것만 응답 큐(ConfigSyncQueue — 토큰 버킷)에 CommandRetryJob 으로 넣는다.

자격(core/command_rules.retry_eligible 과 같은 규칙):
  status ∈ {pending, EXPIRED} · 명령 미종료 · now < sent_at + dur(auto 는 + COMMAND_TIMEOUT)
  · attempts < COMMAND_MAX_ATTEMPTS · last_sent_at < now - COMMAND_RETRY_MIN_SEC
  · 그 단말에 **더 새 명령(seq 큰 대상 행)이 없다** — 옛 소등을 새 점등 뒤에 다시 보내면
    단말은 옛 명령을 마지막으로 적용한다(같은 계층은 나중 것이 덮는다, §3.10.8).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Integer, and_, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.constants import RETRYABLE_STATUSES, MsgType
from app.db import session_scope
from app.models.command import Command, CommandTarget
from app.mqtt.config_sync import CommandRetryJob, ConfigSyncQueue

log = logging.getLogger(__name__)


def _valid_until_sql():
    """command_rules.command_valid_until 의 SQL 판."""
    dur = func.coalesce(Command.payload["dur"].astext.cast(Integer), settings.command_timeout_sec)
    return Command.sent_at + dur * text("interval '1 second'")


def _not_superseded_sql():
    newer = aliased(CommandTarget)
    return ~exists().where(and_(newer.uuid == CommandTarget.uuid, newer.seq > CommandTarget.seq))


async def claim_targets(
    db: AsyncSession, *, now: dt.datetime, uuids: Iterable[str] | None = None,
    seq: int | None = None, manual: bool = False,
) -> list[tuple[int, str, int, dict[str, Any]]]:
    """재발송할 대상을 선점하고 (seq, uuid, 새 attempts, command.payload) 목록을 돌려준다.

    manual=True(관리자 재시도 버튼)는 시도 상한·RETRY_MIN 을 보지 않는다 — 사람이 직접 누른 것.
    나머지 조건(미종료·유효·pending/EXPIRED·더 새 명령 없음)은 같다.
    """
    conds = [
        CommandTarget.seq == Command.seq,
        CommandTarget.status.in_(sorted(RETRYABLE_STATUSES)),
        Command.finished_at.is_(None),
        Command.type == MsgType.COMMAND.value,
        _valid_until_sql() > now,
        _not_superseded_sql(),
    ]
    if uuids is not None:
        conds.append(CommandTarget.uuid.in_(list(uuids)))
    if seq is not None:
        conds.append(CommandTarget.seq == seq)
    if not manual:
        conds.append(CommandTarget.attempts < settings.command_max_attempts)
        retry_min = dt.timedelta(seconds=settings.command_retry_min_sec)
        conds.append(
            (CommandTarget.last_sent_at.is_(None)) | (CommandTarget.last_sent_at < now - retry_min)
        )
    stmt = (
        update(CommandTarget)
        .where(*conds)
        .values(attempts=CommandTarget.attempts + 1, last_sent_at=now)
        .returning(CommandTarget.seq, CommandTarget.uuid, CommandTarget.attempts, Command.payload)
        .execution_options(synchronize_session=False)
    )
    rows = (await db.execute(stmt)).all()
    # 같은 단말에 여러 건이면 옛것부터 — 나중 것이 마지막에 적용되게.
    return sorted(((int(s), u, int(a), p) for s, u, a, p in rows), key=lambda r: (r[1], r[0]))


async def pending_uuids(db: AsyncSession) -> set[str]:
    """재시도 후보가 남은 uuid 전부(시도 상한 미만, 미종료 명령). warm/rebuild 공용."""
    stmt = (
        select(CommandTarget.uuid).distinct()
        .join(Command, Command.seq == CommandTarget.seq)
        .where(
            Command.finished_at.is_(None),
            Command.type == MsgType.COMMAND.value,
            CommandTarget.status.in_(sorted(RETRYABLE_STATUSES)),
            CommandTarget.attempts < settings.command_max_attempts,
        )
    )
    return {row[0] for row in await db.execute(stmt)}


class CommandRetrier:
    def __init__(self, config_sync: ConfigSyncQueue | None) -> None:
        self._config_sync = config_sync
        self._pending: set[str] = set()

    # ── 후보 집합 ───────────────────────────────────────────────────────
    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def has_pending(self, uuid: str) -> bool:
        return uuid in self._pending

    def mark(self, uuids: Iterable[str]) -> None:
        """명령 발송·수동 재시도 뒤 부른다. 롤백돼도 해가 없다(한 번 더 조회할 뿐)."""
        self._pending.update(uuids)

    async def rebuild(self, db: AsyncSession) -> int:
        """DB 기준으로 다시 맞춘다(기동 시·종료 타이머마다)."""
        self._pending = await pending_uuids(db)
        return len(self._pending)

    # ── 단말 송신 직후 ──────────────────────────────────────────────────
    async def on_device_messages(self, uuids: Iterable[str], *, reason: str) -> int:
        """REGISTER 핸들러·TM flush 가 (커밋 뒤) 부른다. 큐에 넣은 건수. 예외를 올리지 않는다."""
        candidates = [u for u in uuids if u in self._pending]
        if not candidates or self._config_sync is None:
            return 0
        now = dt.datetime.now(dt.timezone.utc)
        try:
            async with session_scope() as db:
                claimed = await claim_targets(db, now=now, uuids=candidates)
        except Exception:  # noqa: BLE001 - 수신 경로를 죽이지 않는다. 다음 송신 때 다시.
            log.exception("COMMAND 재시도 선점 실패 (%d대)", len(candidates))
            return 0
        self.enqueue(claimed, reason=reason)
        return len(claimed)

    def enqueue(self, claimed: list[tuple[int, str, int, dict[str, Any]]], *, reason: str) -> None:
        """선점 결과 → 응답 큐. 선점이 **커밋된 뒤** 불러야 한다(ACK 가 먼저 와도 행이 맞게)."""
        if self._config_sync is None:
            return
        for seq, uuid, attempt, payload in claimed:
            self._config_sync.offer_command_retry(
                CommandRetryJob(seq=seq, uuid=uuid, payload=dict(payload), attempt=attempt,
                                reason=reason)
            )
