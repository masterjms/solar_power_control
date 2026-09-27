"""S-23 단말 설정 요청 수명 — 발송 준비 · 재발송(새 seq) · TIMEOUT · 응답 반영.

ADR-007, UI_항목_명세 8.4~8.5. 요청 하나 = device_settings.pending_* 한 벌.
단말당 동시에 하나만(API 가 SETTINGS_PENDING 으로 막는다).

관리자 읽기/쓰기(API)
    begin_request(): seq(cmd_seq) → command 행(type SETTINGS_GET|SET) → pending_* 기록
    (SET 이면 sync=writing). **커밋한 뒤 발행**(COMMAND 와 같은 이유 — 시뮬레이터는 수 ms 안에
    답한다).
무응답 30초
    타이머(5초 간격) → 새 seq·새 command 행으로 재발송(옛 행은 result RESENT).
    3회 다 썼으면 TIMEOUT: last_result=TIMEOUT, pending 해제, writing 이면 원래 sync.
단말이 뭔가 보냄(REGISTER·TELEMETRY 직후, §1.1.10)
    마지막 발송에서 5초 이상 지났으면 30초를 기다리지 않고 바로 재발송. 메모리 집합 `_pending` 에
    없는 uuid 는 쿼리가 없다(COMMAND 자동 재시도와 같은 모양, mqtt/command_retry).
SETTINGS / SETTINGS_ACK
    handlers 가 apply_report / apply_ack 로 반영하고 pending 을 푼다.

재발송은 ConfigSyncQueue(토큰 버킷)를 탄다 — 선점(새 seq·attempts+1)을 **커밋한 뒤** 큐에 넣는다.

늦은 응답: 재발송 뒤 첫 seq 의 응답이 늦게 올 수 있다. 같은 요청
(첫 seq ≤ seq ≤ pending_seq, 같은 종류)이면 그 응답으로 요청을 끝낸다 — SET 은 몸통이 같으므로
어느 seq 에 대한 OK 든 결과가 같다. 끝난 요청의 늦은 응답은 그 뒤 **다른** 요청이 없을 때만
반영한다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core import settings_rules as rules
from app.core.metrics import metrics
from app.db import session_scope
from app.models.command import Command
from app.models.settings import DeviceSettings, DeviceSettingsHistory
from app.mqtt import topics
from app.mqtt.config_sync import ConfigSyncQueue, SettingsJob
from app.mqtt.publisher import check_size, encode

log = logging.getLogger(__name__)

BY_DEVICE_READ = "device_read"
BY_SERVER_WRITE = "server_write"
#: 요청 행이 대체됨(새 seq 로 재발송) — command.result 값.
RESULT_RESENT = "RESENT"


# ── 행 헬퍼 ──────────────────────────────────────────────────────────────
async def get_or_create(db: AsyncSession, uuid: str, *, lock: bool = True) -> DeviceSettings:
    """device_settings 행(없으면 unknown 으로 만든다). device 행은 이미 있어야 한다(FK)."""
    await db.execute(
        pg_insert(DeviceSettings).values(uuid=uuid).on_conflict_do_nothing()
    )
    stmt = select(DeviceSettings).where(DeviceSettings.uuid == uuid)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()
    return row


def db_values(row: DeviceSettings) -> dict[str, int | None]:
    return {k: getattr(row, k) for k in rules.item_keys()}


def db_table(row: DeviceSettings) -> dict[str, Any] | None:
    if row.tbl_lat_e6 is None or row.tbl_lon_e6 is None:
        return None
    return {"region": row.tbl_region, "lat_e6": row.tbl_lat_e6, "lon_e6": row.tbl_lon_e6,
            "on": row.tbl_on, "off": row.tbl_off, "src": row.tbl_src, "crc": row.tbl_crc}


def tbl_note(tbl: dict[str, Any] | None) -> str | None:
    if tbl is None:
        return None
    keep = {k: tbl.get(k) for k in (*rules.TBL_KEYS, "src", "crc") if k in tbl}
    return json.dumps(keep, ensure_ascii=False, separators=(",", ":"))


def _tbl_conditions(tbl: dict[str, Any] | None) -> tuple | None:
    if tbl is None:
        return None
    return tuple(tbl.get(k) for k in rules.TBL_KEYS)


def _history(
    uuid: str, by: str, now: dt.datetime, old: dict[str, Any | None], new: dict[str, int]
) -> list[dict[str, Any]]:
    return [
        {"uuid": uuid, "changed_at": now, "by": by, "key": k, "old": old.get(k), "new": new[k],
         "note": None}
        for k in rules.item_keys() if old.get(k) != new[k]
    ]


async def write_history(db: AsyncSession, rows: list[dict[str, Any]]) -> None:
    # multi-row VALUES 는 첫 행의 키로 컬럼을 정한다 — 모든 행에 같은 키를 채운다.
    if rows:
        full = [{"old": None, "new": None, "note": None, **row} for row in rows]
        await db.execute(pg_insert(DeviceSettingsHistory).values(full))


def _set_values(row: DeviceSettings, values: dict[str, int]) -> None:
    for k in rules.item_keys():
        setattr(row, k, int(values[k]))


def _set_table(row: DeviceSettings, tbl: dict[str, Any], *, src: int | None) -> None:
    row.tbl_region = tbl.get("region")
    row.tbl_lat_e6 = tbl.get("lat_e6")
    row.tbl_lon_e6 = tbl.get("lon_e6")
    row.tbl_on = tbl.get("on")
    row.tbl_off = tbl.get("off")
    row.tbl_src = src
    crc = tbl.get("crc")
    row.tbl_crc = str(crc).upper() if crc is not None else None


def _clear_pending(row: DeviceSettings) -> None:
    row.pending_seq = None
    row.pending_kind = None
    row.pending_body = None
    row.pending_sent_at = None
    row.pending_attempts = None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# ── 발송 준비 (API) ──────────────────────────────────────────────────────
async def _new_command(
    db: AsyncSession, uuid: str, kind: str, body: dict[str, Any] | None, by: str,
    now: dt.datetime,
) -> dict[str, Any]:
    seq = await ids.next_cmd_seq(db)
    payload = rules.payload_for(kind, seq=seq, body=body)
    check_size(topics.device_cmd(uuid), encode(payload))  # 900B 초과면 PayloadTooLarge(버그)
    db.add(Command(
        seq=seq, target_kind="device", target_id=uuid, type=kind, payload=payload,
        expected_count=1, sent_at=now, created_by=by, topics=[topics.device_cmd(uuid)],
    ))
    return payload


async def begin_request(
    db: AsyncSession, row: DeviceSettings, kind: str, body: dict[str, Any] | None, by: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """새 요청 — seq 발번·command 행·pending 기록. 보낼 payload 를 돌려준다(커밋은 호출부)."""
    payload = await _new_command(db, row.uuid, kind, body, by, now)
    row.pending_seq = payload["seq"]
    row.pending_kind = kind
    row.pending_body = {"body": body, "prev_sync": row.sync, "by": by,
                        "first_seq": payload["seq"]}
    row.pending_sent_at = now
    row.pending_attempts = 1
    if kind == rules.KIND_SET:
        row.sync = rules.SYNC_WRITING
    return payload


def cancel_request(row: DeviceSettings) -> None:
    """발행 실패 보상 — pending 해제, writing 이면 원래 sync."""
    prev = (row.pending_body or {}).get("prev_sync")
    if row.sync == rules.SYNC_WRITING:
        row.sync = rules.restore_sync(prev)
    _clear_pending(row)


# ── 재발송 · TIMEOUT ─────────────────────────────────────────────────────
async def claim(
    db: AsyncSession, *, now: dt.datetime, uuids: Iterable[str] | None, device_trigger: bool,
    reason: str,
) -> list[SettingsJob]:
    """대기 중인 요청을 판정해 재발송분은 새 seq 로 선점하고, 소진분은 TIMEOUT 으로 닫는다."""
    stmt = select(DeviceSettings).where(DeviceSettings.pending_seq.is_not(None))
    if uuids is not None:
        stmt = stmt.where(DeviceSettings.uuid.in_(list(uuids)))
    rows = (await db.execute(
        stmt.with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )).scalars().all()
    jobs: list[SettingsJob] = []
    for row in rows:
        sent_at = row.pending_sent_at or now
        decision = rules.resend_decision(
            attempts=row.pending_attempts or 1,
            sent_at_age_sec=(now - sent_at).total_seconds(), device_trigger=device_trigger,
        )
        if decision == rules.RESEND_WAIT:
            continue
        old_seq = row.pending_seq
        if decision == rules.RESEND_GIVE_UP:
            await _finish_command(db, old_seq, rules.RESULT_TIMEOUT, now)
            log.warning("%s %s 3회 무응답 — TIMEOUT (seq=%s)", row.pending_kind, row.uuid, old_seq)
            row.last_result = rules.RESULT_TIMEOUT
            row.last_result_at = now
            cancel_request(row)
            metrics.settings_timeout += 1
            continue
        meta = dict(row.pending_body or {})
        await _finish_command(db, old_seq, RESULT_RESENT, now)
        payload = await _new_command(db, row.uuid, str(row.pending_kind), meta.get("body"),
                                     str(meta.get("by") or BY_SERVER_WRITE), now)
        row.pending_seq = payload["seq"]
        row.pending_sent_at = now
        row.pending_attempts = (row.pending_attempts or 1) + 1
        jobs.append(SettingsJob(uuid=row.uuid, payload=payload,
                                attempt=row.pending_attempts, reason=reason))
    return jobs


async def _finish_command(
    db: AsyncSession, seq: int | None, result: str, now: dt.datetime, *, acked: bool = False
) -> None:
    if seq is None:
        return
    values: dict[str, Any] = {"finished_at": now, "result": result}
    if acked:
        values["acked_count"] = 1
    await db.execute(
        update(Command).where(Command.seq == seq, Command.finished_at.is_(None)).values(**values)
    )


async def pending_uuids(db: AsyncSession) -> set[str]:
    rows = await db.execute(
        select(DeviceSettings.uuid).where(DeviceSettings.pending_seq.is_not(None))
    )
    return {r[0] for r in rows}


# ── 응답 반영 (handlers) ─────────────────────────────────────────────────
def _request_of(row: DeviceSettings, seq: int, kind: str) -> bool:
    """seq 가 지금 대기 중인 요청(재발송 포함)의 것인가."""
    if row.pending_seq is None or row.pending_kind != kind:
        return False
    first = _int((row.pending_body or {}).get("first_seq")) or row.pending_seq
    return first <= seq <= row.pending_seq


async def _newer_request_exists(db: AsyncSession, uuid: str, seq: int) -> bool:
    """이 seq 뒤에 이 단말로 보낸 **다른** SETTINGS 요청이 있나 — 옛 요청의 늦은 응답이 새 결과를
    덮지 않게. 같은 요청의 재발송(종류·몸통이 같고 seq 만 다름)은 다른 요청으로 치지 않는다."""
    kinds = (rules.KIND_GET, rules.KIND_SET)
    this = (await db.execute(
        select(Command.type, Command.payload).where(Command.seq == seq)
    )).first()
    rows = (await db.execute(
        select(Command.type, Command.payload).where(
            Command.target_id == uuid, Command.type.in_(kinds), Command.seq > seq,
        ).order_by(Command.seq).limit(20)
    )).all()

    def shape(t: Any, p: Any) -> tuple:
        p = p or {}
        return (t, json.dumps(p.get("v"), sort_keys=True), json.dumps(p.get("tbl"), sort_keys=True))

    mine = shape(*this) if this is not None else None
    return any(shape(t, p) != mine for t, p in rows)


async def apply_report(
    db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime
) -> str | None:
    """SETTINGS (명세 8.4 읽기 응답). 반환: 판정(first_read/synced/device_changed) 또는
    None(반영 안 함).

    · pending GET 과 맞으면 요청을 끝낸다. 안 맞아도 새 보고로 받는다(로그).
    · SET 을 기다리는 중(writing)에 온 보고는 쓰기 결과를 흐리지 않도록 last_report 에만 둔다.
    """
    seq = _int(data.get("seq"))
    row = await get_or_create(db, uuid)
    matching = seq is not None and _request_of(row, seq, rules.KIND_GET)
    if not matching:
        log.info("SETTINGS %s seq=%s — 대기 중인 읽기와 안 맞음(pending=%s %s), 새 보고로 받는다",
                 uuid, seq, row.pending_kind, row.pending_seq)
    if row.pending_kind == rules.KIND_SET:
        row.last_report = data
        return None
    if not matching and seq is not None and await _newer_request_exists(db, uuid, seq):
        log.warning("SETTINGS %s seq=%s — 그 뒤 보낸 요청이 있어 옛 보고로 보고 반영 안 함",
                    uuid, seq)
        return None

    values = rules.parse_values(data.get("v"))
    if values is None:
        log.warning("SETTINGS %s seq=%s — v 가 25개 정수가 아님, 반영 안 함", uuid, seq)
        return None
    sh = str(data.get("sh") or "").upper() or None
    if sh is not None and sh != rules.fingerprint(values):
        log.warning("SETTINGS %s — 단말 sh %s 가 보고 값의 지문 %s 과 다름(항목 정의 불일치?)",
                    uuid, sh, rules.fingerprint(values))
    tbl = data.get("tbl") if isinstance(data.get("tbl"), dict) else None
    dev = data.get("dev") if isinstance(data.get("dev"), dict) else None

    old = db_values(row)
    decision = rules.on_report(db_values=old, reported_sh=sh, reported_values=values)
    history: list[dict[str, Any]] = []
    if decision == rules.READ_FIRST:
        _set_values(row, values)
        history += _history(uuid, BY_DEVICE_READ, now, old, values)
    if tbl is not None and decision in (rules.READ_FIRST, rules.READ_SAME):
        before = db_table(row)
        if _tbl_conditions(before) != _tbl_conditions(tbl) or \
                (before or {}).get("crc") != str(tbl.get("crc") or "").upper():
            history.append({"uuid": uuid, "changed_at": now, "by": BY_DEVICE_READ, "key": "tbl",
                            "old": None, "new": None, "note": tbl_note(tbl)})
        _set_table(row, tbl, src=_int(tbl.get("src")))
    if dev is not None:
        row.dip = _int(dev.get("dip"))
        row.bat = _int(dev.get("bat"))
    row.sh_device = sh or rules.fingerprint(values)
    if tbl is not None and _int(tbl.get("ss")) is not None:
        row.ss_known = _int(tbl.get("ss"))
    row.read_at = now
    row.last_report = data
    row.sync = rules.SYNC_DEVICE_CHANGED if decision == rules.READ_CHANGED else rules.SYNC_SYNCED
    if matching:
        await _finish_command(db, row.pending_seq, "OK", now, acked=True)
        if seq != row.pending_seq:
            await _finish_command(db, seq, "OK", now, acked=True)
        _clear_pending(row)
        row.last_result = "OK"
        row.last_result_at = now
    await write_history(db, history)
    return decision


async def apply_ack(
    db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime
) -> str | None:
    """SETTINGS_ACK (명세 8.4 쓰기 응답, 읽기의 STATE 거부도 여기). 반환: 판정 또는 None."""
    seq = _int(data.get("seq"))
    result = str(data.get("result") or "").strip().upper()
    if seq is None:
        return None
    cmd = (await db.execute(
        select(Command.type, Command.target_id, Command.payload, Command.created_by)
        .where(Command.seq == seq)
    )).first()
    if cmd is None or cmd[0] not in (rules.KIND_GET, rules.KIND_SET) or cmd[1] != uuid:
        metrics.settings_mismatch += 1
        log.warning("모르는 seq 의 SETTINGS_ACK %s seq=%s (지운 요청·다른 단말·위조)", uuid, seq)
        return None
    kind = str(cmd[0])
    row = await get_or_create(db, uuid)
    matching = _request_of(row, seq, kind)
    await _finish_command(db, seq, result or "?", now, acked=True)
    if not matching and row.pending_seq is not None:
        # 다른 요청이 진행 중이다 — 그 결과를 기다린다. 옛 요청의 늦은 응답은 이력(device_event)만.
        log.warning("SETTINGS_ACK %s seq=%s — 진행 중인 다른 요청(%s %s)이 있어 반영 안 함",
                    uuid, seq, row.pending_kind, row.pending_seq)
        return None
    if not matching and await _newer_request_exists(db, uuid, seq):
        log.warning("SETTINGS_ACK %s seq=%s — 그 뒤 보낸 요청이 있어 늦은 응답으로 보고 반영 안 함",
                    uuid, seq)
        return None
    if result not in rules.ACK_RESULTS:
        log.warning("SETTINGS_ACK 알 수 없는 result=%r %s seq=%s", result, uuid, seq)

    meta = dict(row.pending_body or {}) if matching else {}
    payload = cmd[2] or {}
    body = {"v": payload.get("v"), "tbl": payload.get("tbl")} if kind == rules.KIND_SET else None
    sent_values = rules.parse_values(body["v"]) if body else None
    sent_sh = rules.fingerprint(sent_values) if sent_values else None
    decision = rules.on_ack(result=result, ack_sh=str(data.get("sh") or "") or None,
                            sent_sh=sent_sh)
    sh = str(data.get("sh") or "").upper() or None
    ss = _int(data.get("ss"))

    if decision == rules.ACK_APPLY and sent_values is not None:
        by = str(meta.get("by") or cmd[3] or BY_SERVER_WRITE)
        old = db_values(row)
        history = _history(uuid, by, now, old, sent_values)
        _set_values(row, sent_values)
        tbl = body.get("tbl") if body else None
        if isinstance(tbl, dict):
            _set_table(row, tbl, src=2)
            history.append({"uuid": uuid, "changed_at": now, "by": by, "key": "tbl",
                            "old": None, "new": None, "note": tbl_note({**tbl, "src": 2})})
        await write_history(db, history)
        row.sync = rules.SYNC_SYNCED
    elif decision == rules.ACK_CHANGED:
        log.warning("SETTINGS_ACK OK 인데 sh %s ≠ 보낸 값 %s — device_changed(다시 읽기 권장) %s",
                    sh, sent_sh, uuid)
        row.sync = rules.SYNC_DEVICE_CHANGED
        row.last_report = None
    else:
        if row.sync == rules.SYNC_WRITING:
            row.sync = rules.restore_sync(meta.get("prev_sync"))
    if result == "OK":
        if sh is not None:
            row.sh_device = sh
        if ss is not None:
            row.ss_known = ss
    row.last_result = result or "?"
    row.last_result_at = now
    if matching:
        if row.pending_seq != seq:
            await _finish_command(db, row.pending_seq, result or "?", now)
        _clear_pending(row)
    return decision


# ── 후보 집합 · 타이머 ───────────────────────────────────────────────────
class SettingsSync:
    """메모리 후보 집합 + 재발송 진입점. main 이 하나 만들어 핸들러·버퍼·API 에 나눠 준다."""

    #: 이 틱마다 한 번은 후보가 없어도 DB 로 다시 맞춘다(5초 × 12 = 1분).
    REBUILD_EVERY = 12

    def __init__(self, config_sync: ConfigSyncQueue | None) -> None:
        self._config_sync = config_sync
        self._pending: set[str] = set()
        self._ticks = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def mark(self, uuid: str) -> None:
        self._pending.add(uuid)

    def unmark(self, uuid: str) -> None:
        self._pending.discard(uuid)

    async def rebuild(self, db: AsyncSession) -> int:
        self._pending = await pending_uuids(db)
        return len(self._pending)

    def _enqueue(self, jobs: list[SettingsJob]) -> None:
        if self._config_sync is None:
            return
        for job in jobs:
            self._config_sync.offer_settings(job)

    async def tick(self) -> None:
        """스케줄러(5초). 30초 무응답 재발송·TIMEOUT. 후보가 없으면 DB 를 안 본다
        (1분에 한 번은 DB 로 재조정)."""
        self._ticks += 1
        if not self._pending and self._ticks % self.REBUILD_EVERY:
            return
        now = dt.datetime.now(dt.timezone.utc)
        try:
            async with session_scope() as db:
                jobs = await claim(db, now=now, uuids=None, device_trigger=False, reason="timer")
            async with session_scope() as db:
                await self.rebuild(db)
        except Exception:  # noqa: BLE001
            log.exception("SETTINGS 재발송 판정 실패 (5초 뒤 다시)")
            return
        self._enqueue(jobs)

    async def on_device_messages(self, uuids: Iterable[str], *, reason: str) -> int:
        """REGISTER·TM flush 가 커밋 뒤 부른다(§1.1.10). 예외를 올리지 않는다."""
        candidates = [u for u in uuids if u in self._pending]
        if not candidates:
            return 0
        now = dt.datetime.now(dt.timezone.utc)
        try:
            async with session_scope() as db:
                jobs = await claim(db, now=now, uuids=candidates, device_trigger=True,
                                   reason=reason)
        except Exception:  # noqa: BLE001
            log.exception("SETTINGS 재발송 선점 실패 (%d대)", len(candidates))
            return 0
        self._enqueue(jobs)
        return len(jobs)
