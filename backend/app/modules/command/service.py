"""원격 명령 서비스 — 미리보기 · 발송 · 이력 · 수동 재시도 (ADR-005, 사양서 §3.10 · §17).

발송 순서(POST /api/commands):
  1. 대상 해석 — device / node(말단·상위) / all(최고관리자만). topic 목록과 "오늘 밤" 좌표도 여기서.
  2. 검증(core/command_rules) → 유지시간 확정(tonight 은 suntable).
  3. 범위 안 ACTIVE 단말 수. 0 이면 409 NO_TARGETS — seq 를 뽑기 전에 막는다.
  4. seq(DB 시퀀스 cmd_seq) → payload → command 행 + command_target 스냅숏(INSERT … SELECT 한 문장).
  5. **커밋한 뒤 발행**한다. 로컬 시뮬레이터는 수 ms 안에 COMMAND_ACK 를 돌려주는데, 발행을 먼저
     하고 응답 뒤(미들웨어)에서 커밋하면 ACK 핸들러가 아직 없는 대상 행을 보고 "모르는 seq" 로
     버린다. 발행이 실패하면 방금 쓴 행을 지우고(보상) 503 MQTT_UNAVAILABLE.
  6. 개별(device) 발송만 device_event(COMMAND_SENT). 그룹·전체는 command 행 하나로 충분하다.
  7. 대상 uuid 를 재시도 후보 집합에 넣는다(단말이 다음에 뭔가 보내면 필요 시 개별 재발송).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import BigInteger, Integer, and_, case, delete, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import UUID_RE, DeviceState, EventKind, MsgType, TargetStatus
from app.core import ids, presence
from app.core.auth import Principal, require_super
from app.core.command_rules import (
    CommandInvalid,
    CommandSpec,
    command_topics,
    tonight_seconds,
    validate_command,
)
from app.core.region_tree import Tree
from app.errors import (
    CommandFinished,
    CommandNotFound,
    DeviceNotFound,
    MqttUnavailable,
    NoTargets,
    RegionNotFound,
    ValidationFailed,
)
from app.models.command import Command, CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.modules.command.schemas import (
    CommandDetail,
    CommandIn,
    CommandItem,
    CommandOut,
    PreviewOut,
    RetryIn,
    RetryOut,
    TargetOut,
    TargetRow,
)
from app.modules.region.service import load_tree
from app.mqtt.command_retry import CommandRetrier, claim_targets
from app.mqtt.config_sync import command_sent_event
from app.mqtt.publisher import MqttPublisher, check_size, command_payload, encode, kst_ts

log = logging.getLogger(__name__)

ALL_LABEL = "전체"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class Resolved:
    kind: str
    target_id: str | None
    label: str
    topics: list[str]
    #: 대상 범위 SQL 조건(ACTIVE 여부는 따로 건다).
    scope: Any
    #: "오늘 밤" 좌표(없으면 DEFAULT_LAT/LON).
    coords: tuple[float, float] | None


# ── 대상 해석 ────────────────────────────────────────────────────────────
def device_label(uuid: str, site: str | None) -> str:
    return f"{site} ({uuid})" if site else uuid


async def _resolve(db: AsyncSession, body: CommandIn, me: Principal, tree: Tree) -> Resolved:
    kind = body.target.kind
    if kind == "all":
        require_super(me, action="command.all")
        return Resolved("all", None, ALL_LABEL, command_topics("all", None, tree),
                        literal(True), None)
    if kind == "node":
        try:
            node_id = int(str(body.target.id))
        except (TypeError, ValueError) as e:
            raise ValidationFailed("target.id 는 트리 노드 id(숫자)",
                                   detail={"field": "target.id"}) from e
        node = tree.get(node_id)
        if node is None:
            raise RegionNotFound(detail={"id": node_id})
        # 단말은 말단에만 배정되지만, 하위 전체 id 로 걸어도 결과는 같고 규칙이 단순하다.
        scope = Device.node_id.in_(tree.subtree_ids(node_id))
        return Resolved("node", str(node_id), tree.path_name(node_id) or node.name,
                        command_topics("node", str(node_id), tree), scope,
                        tree.coords_for(node_id))
    uuid = str(body.target.id or "").strip().upper()
    if not UUID_RE.match(uuid):
        raise ValidationFailed("target.id 는 24자리 16진수 UUID", detail={"field": "target.id"})
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    coords = (device.lat, device.lon) if device.lat is not None and device.lon is not None \
        else tree.coords_for(device.node_id)
    return Resolved("device", uuid, device_label(uuid, device.site),
                    command_topics("device", uuid, tree), Device.uuid == uuid, coords)


def _spec(body: CommandIn) -> CommandSpec:
    try:
        return validate_command(
            act=body.act, ch=body.ch, pwm=body.pwm, dur=body.dur, dur_preset=body.dur_preset,
            exp=body.exp, default_exp=settings.command_exp_sec,
        )
    except CommandInvalid as e:
        raise ValidationFailed(e.message, detail={"field": e.field}) from e


def _dur(spec: CommandSpec, target: Resolved, now: dt.datetime) -> int | None:
    if spec.act == "auto":
        return None
    if spec.needs_tonight:
        lat, lon = target.coords or (settings.default_lat, settings.default_lon)
        return tonight_seconds(now, lat, lon)
    return spec.dur


def _low_battery_sql():
    """last_telemetry.er & 1 (BATT_LOW). er 가 숫자가 아니면 0 으로 본다(캐스트 오류 방지)."""
    er = Device.last_telemetry["er"].astext
    return case((er.op("~")("^[0-9]+$"), er.cast(BigInteger).op("&")(1)), else_=0) == 1


async def _stats(db: AsyncSession, scope: Any, now: dt.datetime) -> dict[str, int]:
    active = Device.state == DeviceState.ACTIVE.value
    row = (await db.execute(
        select(
            func.count().filter(active),
            func.count().filter(and_(active, presence.online_clause(now))),
            func.count().filter(and_(active, _low_battery_sql())),
            func.count().filter(~active),
        ).select_from(Device).where(scope)
    )).one()
    expected, online, low, not_active = (int(v or 0) for v in row)
    return {"expected": expected, "online": online, "offline": expected - online,
            "low_battery": low, "not_active": not_active}


def _build_payload(spec: CommandSpec, *, seq: int, now: dt.datetime, dur: int | None):
    payload = command_payload(seq=seq, ts=kst_ts(now), exp=spec.exp, act=spec.act, ch=spec.ch,
                              pwm=spec.pwm, dur=dur)
    check_size("cmd", encode(payload))  # 384B 초과면 PayloadTooLarge(서버 버그)
    return payload


# ── 미리보기 · 발송 ──────────────────────────────────────────────────────
async def preview(db: AsyncSession, body: CommandIn, me: Principal) -> PreviewOut:
    tree = await load_tree(db)
    target = await _resolve(db, body, me, tree)
    spec = _spec(body)
    now = _now()
    dur = _dur(spec, target, now)
    stats = await _stats(db, target.scope, now)
    payload = _build_payload(spec, seq=0, now=now, dur=dur)
    payload.pop("seq")
    return PreviewOut(**stats, topics=target.topics, dur=dur, payload=payload)


async def create(
    db: AsyncSession, body: CommandIn, me: Principal, publisher: MqttPublisher,
    retrier: CommandRetrier | None,
) -> CommandOut:
    tree = await load_tree(db)
    target = await _resolve(db, body, me, tree)
    spec = _spec(body)
    now = _now()
    dur = _dur(spec, target, now)
    stats = await _stats(db, target.scope, now)
    if stats["expected"] == 0 or not target.topics:
        raise NoTargets(detail={"target": {"kind": target.kind, "id": target.target_id},
                                "not_active": stats["not_active"]})
    if not publisher.connection.is_connected:
        raise MqttUnavailable()

    seq = await ids.next_cmd_seq(db)
    payload = _build_payload(spec, seq=seq, now=now, dur=dur)
    db.add(Command(
        seq=seq, target_kind=target.kind, target_id=target.target_id,
        type=MsgType.COMMAND.value, payload=payload, expected_count=0, sent_at=now,
        created_by=me.user, topics=target.topics, exp=spec.exp,
    ))
    await db.flush()
    snap = select(
        literal(seq, BigInteger), Device.uuid, literal(TargetStatus.PENDING.value),
        literal(1, Integer), literal(now),
    ).where(target.scope, Device.state == DeviceState.ACTIVE.value)
    inserted = await db.execute(
        pg_insert(CommandTarget)
        .from_select(["seq", "uuid", "status", "attempts", "last_sent_at"], snap)
        .returning(CommandTarget.uuid)
    )
    uuids = [r[0] for r in inserted.all()]
    if not uuids:  # 3 과 4 사이에 승인이 풀렸다
        raise NoTargets(detail={"target": {"kind": target.kind, "id": target.target_id}})
    cmd = await db.get(Command, seq)
    cmd.expected_count = len(uuids)
    await db.commit()

    try:
        for topic in target.topics:
            await publisher.publish_command(topic=topic, payload=payload)
    except Exception as e:
        # 보상: 행을 지운다(대상은 CASCADE). seq 는 되감지 않는다 — 일부 topic 은 나갔을 수 있고
        # 단말이 본 번호를 다시 쓰면 중복 실행 방지가 깨진다.
        log.exception("COMMAND seq=%d 발행 실패 — 명령 행 삭제", seq)
        await db.execute(delete(Command).where(Command.seq == seq))
        await db.commit()
        if isinstance(e, MqttUnavailable):
            raise
        raise MqttUnavailable(detail={"seq": seq}) from e

    if target.kind == "device":
        await db.execute(pg_insert(DeviceEvent).values(
            uuid=target.target_id, kind=EventKind.COMMAND_SENT.value,
            payload=command_sent_event(target.topics[0], payload, attempt=1, by=me.user),
            received_at=now,
        ))
    if retrier is not None:
        retrier.mark(uuids)
    log.info("COMMAND seq=%d %s %s act=%s → topic %d개, 대상 %d대 (by %s)",
             seq, target.kind, target.target_id, spec.act, len(target.topics), len(uuids),
             me.user)
    return CommandOut(
        seq=seq, target=TargetOut(kind=target.kind, id=target.target_id, label=target.label),
        topics=target.topics, payload=payload, expected=len(uuids), sent_at=now,
        created_by=me.user,
    )


# ── 이력 ─────────────────────────────────────────────────────────────────
def _empty_counts() -> dict[str, int]:
    return {s.value: 0 for s in TargetStatus}


async def _labels(db: AsyncSession, cmds: list[Command], tree: Tree) -> dict[int, str]:
    uuids = [c.target_id for c in cmds if c.target_kind == "device" and c.target_id]
    sites: dict[str, str | None] = {}
    if uuids:
        sites = dict((await db.execute(
            select(Device.uuid, Device.site).where(Device.uuid.in_(uuids))
        )).all())
    out: dict[int, str] = {}
    for c in cmds:
        if c.target_kind == "all":
            out[c.seq] = ALL_LABEL
        elif c.target_kind == "node":
            try:
                out[c.seq] = tree.path_name(int(c.target_id or "")) or f"노드 {c.target_id}"
            except ValueError:
                out[c.seq] = f"노드 {c.target_id}"
        else:
            out[c.seq] = device_label(c.target_id or "", sites.get(c.target_id or ""))
    return out


def _item(c: Command, label: str, counts: dict[str, int]) -> dict[str, Any]:
    p = c.payload or {}
    return {
        "seq": c.seq, "created_by": c.created_by, "target_kind": c.target_kind,
        "target_id": c.target_id, "target_label": label, "act": p.get("act"),
        "ch": p.get("ch"), "pwm": p.get("pwm"), "dur": p.get("dur"), "sent_at": c.sent_at,
        "finished_at": c.finished_at, "result": c.result, "expected_count": c.expected_count,
        "counts": counts,
    }


async def _counts(db: AsyncSession, seqs: list[int]) -> dict[int, dict[str, int]]:
    out = {s: _empty_counts() for s in seqs}
    if not seqs:
        return out
    rows = await db.execute(
        select(CommandTarget.seq, CommandTarget.status, func.count())
        .where(CommandTarget.seq.in_(seqs))
        .group_by(CommandTarget.seq, CommandTarget.status)
    )
    for seq, status, n in rows:
        out[seq][status] = int(n)
    return out


async def list_commands(
    db: AsyncSession, *, limit: int, uuid: str | None, node_id: int | None
) -> list[CommandItem]:
    """최신순. uuid = 그 단말이 대상 스냅숏에 든 명령 전부(그룹·전체 포함). node_id = 그 노드
    또는 하위 노드를 대상으로 한 명령. PING 은 빼고 COMMAND 만."""
    tree = await load_tree(db)
    stmt = select(Command).where(Command.type == MsgType.COMMAND.value)
    if uuid:
        u = uuid.strip().upper()
        stmt = stmt.where(exists().where(CommandTarget.seq == Command.seq,
                                         CommandTarget.uuid == u))
    if node_id is not None:
        if tree.get(node_id) is None:
            raise RegionNotFound(detail={"id": node_id})
        stmt = stmt.where(Command.target_kind == "node",
                          Command.target_id.in_([str(i) for i in tree.subtree_ids(node_id)]))
    cmds = list((await db.execute(stmt.order_by(Command.seq.desc()).limit(limit))).scalars())
    labels = await _labels(db, cmds, tree)
    counts = await _counts(db, [c.seq for c in cmds])
    return [CommandItem(**_item(c, labels[c.seq], counts[c.seq])) for c in cmds]


async def _get_command(db: AsyncSession, seq: int) -> Command:
    cmd = await db.get(Command, seq)
    if cmd is None or cmd.type != MsgType.COMMAND.value:
        raise CommandNotFound(detail={"seq": seq})
    return cmd


async def get_command(db: AsyncSession, seq: int) -> CommandDetail:
    cmd = await _get_command(db, seq)
    tree = await load_tree(db)
    now = _now()
    labels = await _labels(db, [cmd], tree)
    counts = await _counts(db, [seq])
    rows = await db.execute(
        select(CommandTarget, Device.site, Device.node_id,
               func.coalesce(presence.online_clause(now), False))
        .join(Device, Device.uuid == CommandTarget.uuid, isouter=True)
        .where(CommandTarget.seq == seq)
        .order_by(CommandTarget.uuid)
    )
    targets = []
    for t, site, node_id, online in rows:
        node = tree.get(node_id)
        targets.append(TargetRow(
            uuid=t.uuid, site=site, node_name=node.name if node else None, status=t.status,
            attempts=t.attempts, last_sent_at=t.last_sent_at, acked_at=t.acked_at,
            is_online=bool(online),
        ))
    return CommandDetail(**_item(cmd, labels[seq], counts[seq]), targets=targets)


# ── 수동 재시도 ──────────────────────────────────────────────────────────
async def retry(
    db: AsyncSession, seq: int, body: RetryIn, publisher: MqttPublisher,
    retrier: CommandRetrier | None,
) -> RetryOut:
    """무응답(pending)·EXPIRED 대상만 개별 topic 으로 같은 seq·새 ts. 시도 상한·RETRY_MIN 은
    보지 않는다(사람이 누른 것). 더 새 명령을 받은 단말은 건너뛴다(옛 명령이 새 명령을 덮지 않게).
    발행은 응답 큐(토큰 버킷)로 — 수백 대여도 요청이 오래 걸리지 않는다."""
    cmd = await _get_command(db, seq)
    if cmd.finished_at is not None:
        raise CommandFinished(detail={"seq": seq, "result": cmd.result})
    if not publisher.connection.is_connected:
        raise MqttUnavailable()
    uuids = [u.strip().upper() for u in body.uuids] if body.uuids is not None else None
    claimed = await claim_targets(db, now=_now(), uuids=uuids, seq=seq, manual=True)
    await db.commit()  # 큐가 발행하기 전에 선점을 확정(ACK 가 먼저 와도 대상 행이 맞다)
    if retrier is not None:
        retrier.enqueue(claimed, reason="manual")
        retrier.mark(u for _, u, _, _ in claimed)
    return RetryOut(resent=len(claimed))
