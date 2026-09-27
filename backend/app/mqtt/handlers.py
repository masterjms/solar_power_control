"""단말 → 서버 메시지 처리 (사양서 §1.1.4~§1.1.7, §3, §16).

토픽 kind 로 1차 분기, payload `type` 으로 2차 분기한다.

    register  REGISTER    device upsert(보강 정보, 새 UUID 는 PENDING) + device_event
                          → REGISTER_ACK(retain) **항상** 큐 (RETIRED 였으면 PENDING 으로 되돌린 뒤)
                          → ACTIVE 이고 cv 다르면 CONFIG_SET 큐 (S-10, S-13)
    status    TELEMETRY   TelemetryBuffer 에만 넣는다 (DB 를 만지지 않는다). "TM"/`t:TM` 도 받음
    result    PONG        device_event + command_ack + command 갱신
              CONFIG_ACK  device_event. OK → cv_device/ti_device/ka_device 반영(cv 일치 때만)
                          RANGE → 경고(서버 버그). STATE → 단말이 승인 전이라 함: DB 가 ACTIVE 면
                          REGISTER_ACK 재발행. FLASH → 쿨다운 해제(다음 송신 때 재전송)
              COMMAND_ACK 5차(옛 이름 CMD_ACK 도 받음). device_event(COMMAND_ACK) + command_target
                          상태·acked_count. OK 면 device.override_* 기록(§3.10.8 S-19)
    event     LWT         online=false (presence.apply_presence — 브로커 로그 경로와 같은 함수).
                          last_seen_at 은 건드리지 않는다 — 브로커가 대신 보내는 사망 통지를
                          "방금 통신함"으로 적으면 죽은 단말이 온라인으로 잡힌다
              EV          device_event(kind=ERR)

공통 검증:
    · topic uuid 형식 위반 → malformed_topic, 무시
    · JSON 아님 / dict 아님 → malformed_payload, 무시
    · payload.uuid 가 있고 topic uuid 와 다르면 → uuid_mismatch, 무시 (사양서 §1.1.4)
    · `t` 키(1.0.0 펌웨어) → `type` 으로 정규화하고 uuid 당 한 번 로그

result/event 는 메시지 1건 = 트랜잭션 1개다. 드물고(명령에 대한 응답), 이력 행이라
합칠 수 없다. TELEMETRY 만 버퍼를 탄다.

서버 → 단말 발행은 전부 ConfigSyncQueue(토큰 버킷)를 거친다 — 브로커 재시작 뒤 1만 대
REGISTER 폭주에서 응답을 초당 상한으로 누른다(사양서 §1.1.10 "즉시" 는 50초 안이면 된다).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ACK_RESULTS, DeviceState, EventKind, MsgType, TargetStatus, TopicKind
from app.core import presence
from app.core.command_rules import OverrideState, override_after_ok, override_level_for
from app.core.effective import effective_ka_sql, effective_ti_sql
from app.core.metrics import metrics
from app.db import session_scope
from app.models.command import Command, CommandAck, CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.mqtt import topics
from app.mqtt.command_retry import CommandRetrier
from app.mqtt.config_decide import CONFIG_COLUMNS, DeviceConfigRow, decide_and_enqueue
from app.mqtt.config_sync import ConfigSyncQueue, register_ack_job_for
from app.mqtt.publisher import parse_kst_ts
from app.mqtt.telemetry_buffer import TelemetryBuffer
from app.tasks.command_finisher import finish_due

log = logging.getLogger(__name__)

#: status 토픽에서 Telemetry 로 받아들이는 type 값. 1.1.0+ "TELEMETRY", 초기 2차 "TM".
TELEMETRY_TYPES = frozenset({MsgType.TELEMETRY.value, MsgType.TM.value})
#: result 토픽의 원격 명령 응답. CMD_ACK 는 5차 확정 전 이름이라 같이 받는다.
COMMAND_ACK_TYPES = frozenset({MsgType.COMMAND_ACK.value, MsgType.CMD_ACK.value})


def next_target_status(current: str, result: str) -> str:
    """COMMAND_ACK.result → command_target.status. 한 번 OK 면 내려가지 않는다 — 같은 seq 재발송에
    단말은 처음 결과로 답하지만(최근 8개 기억), 순서가 뒤바뀐 늦은 EXPIRED 가 OK 를 덮으면 안 된다.
    LOCAL → OK 는 허용(현장 조작이 끝나 적용했다는 뜻, §3.10.11)."""
    if current == TargetStatus.OK.value and result != TargetStatus.OK.value:
        return current
    return result


# ── 순수 함수 ────────────────────────────────────────────────────────────
def parse_payload(raw: bytes) -> dict[str, Any] | None:
    """깨진 payload 때문에 수신 루프가 죽지 않도록 None 으로 흡수한다."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def normalize_type(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """1.0.0 펌웨어의 `"t":"TM"` 을 `"type":"TM"` 으로 맞춘다. (정규화된 dict, legacy 여부).

    현장 배포 전 단말이 `type` 만 보내도록 정리되면 이 함수만 지우면 된다.
    """
    if "type" in data:
        return data, False
    if "t" in data:
        normalized = dict(data)
        normalized["type"] = normalized.pop("t")
        return normalized, True
    return data, False


def is_telemetry_type(msg_type: str) -> bool:
    return msg_type in TELEMETRY_TYPES


def dedup_key(uuid: str, kind: str, marker: Any, payload: dict[str, Any]) -> str:
    """`uuid:type:seq-or-cv:sha1(payload)[:16]` (docs/03 device_event).

    payload 내용까지 키에 넣는다 — 같은 seq 에 대한 다른 내용(예: 재시도 결과)은 남기고,
    QoS1 재전송(바이트 단위로 같은 payload)만 걸러진다.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{uuid}:{kind}:{marker if marker is not None else '-'}:{digest}"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


# ── DB 조각 ──────────────────────────────────────────────────────────────
async def _touch_device(
    db: AsyncSession, uuid: str, *, seen_at: dt.datetime | None, **values: Any
) -> None:
    """단말 행 upsert. 처음 보는 uuid 면 여기서 생긴다(state 는 DB 기본값 PENDING).

    seen_at 을 None 으로 주면 last_seen_at 을 건드리지 않는다 — LWT 가 그렇다.
    """
    row: dict[str, Any] = {"uuid": uuid, **values}
    if seen_at is not None:
        row["last_seen_at"] = seen_at
    stmt = pg_insert(Device).values(**row)
    set_ = {k: stmt.excluded[k] for k in row if k not in ("uuid", "last_seen_at")}
    if seen_at is not None:
        set_["last_seen_at"] = func.greatest(Device.last_seen_at, stmt.excluded.last_seen_at)
    set_["updated_at"] = func.now()
    await db.execute(stmt.on_conflict_do_update(index_elements=[Device.uuid], set_=set_))


async def _insert_event(
    db: AsyncSession, *, uuid: str, kind: EventKind, payload: dict[str, Any],
    key: str | None, received_at: dt.datetime,
) -> bool:
    """이력 1행. dedup_key 충돌은 조용히 버린다(같은 메시지의 재전송). 넣었으면 True."""
    stmt = pg_insert(DeviceEvent).values(
        uuid=uuid, kind=kind.value, payload=payload, dedup_key=key, received_at=received_at
    ).on_conflict_do_nothing()
    result = await db.execute(stmt)
    return bool(result.rowcount)


# ── 디스패처 ─────────────────────────────────────────────────────────────
class Dispatcher:
    """MqttConnection 의 on_message. 의존성(버퍼·큐)을 들고 있어 람다 대신 클래스다."""

    def __init__(
        self, *, buffer: TelemetryBuffer | None, config_sync: ConfigSyncQueue | None,
        retrier: CommandRetrier | None = None,
    ) -> None:
        self._buffer = buffer
        self._config_sync = config_sync
        self._retrier = retrier
        #: `t` 키로 보고하는 단말. 로그는 uuid 당 한 번.
        self._legacy_t: set[str] = set()

    async def __call__(self, topic: str, raw: bytes) -> None:
        parsed = topics.parse_inbound(topic)
        if parsed is None:
            metrics.malformed_topic += 1
            log.debug("형식 밖 토픽 무시: %s", topic)
            return
        uuid, kind = parsed

        data = parse_payload(raw)
        if data is None:
            metrics.malformed_payload += 1
            log.warning("payload 파싱 실패 %s (%dB)", topic, len(raw))
            return
        data, legacy = normalize_type(data)
        if legacy and uuid not in self._legacy_t:
            self._legacy_t.add(uuid)
            metrics.legacy_t_devices = len(self._legacy_t)
            log.info("1.0.0 펌웨어(`t` 키) 단말: %s", uuid)

        payload_uuid = data.get("uuid")
        if payload_uuid is not None and str(payload_uuid).upper() != uuid:
            metrics.uuid_mismatch += 1
            log.warning("payload.uuid 불일치 topic=%s payload=%s — 무시", uuid, payload_uuid)
            return

        msg_type = str(data.get("type") or "")
        metrics.received[kind.value] += 1
        metrics.received_type[msg_type or "?"] += 1
        now = dt.datetime.now(dt.timezone.utc)

        if kind is TopicKind.STATUS:
            if is_telemetry_type(msg_type) and self._buffer is not None:
                self._buffer.offer(uuid, payload=data, received_at=now)
            else:
                metrics.unknown_type += 1
                log.warning("status 토픽에 알 수 없는 type=%r (%s)", msg_type, uuid)
            return

        async with session_scope() as db:
            if kind is TopicKind.REGISTER:
                await self.handle_register(db, uuid, data, now)
            elif kind is TopicKind.RESULT:
                await self.handle_result(db, uuid, msg_type, data, now)
            elif kind is TopicKind.EVENT:
                await self.handle_event(db, uuid, msg_type, data, now)

        # 5차 자동 재시도: REGISTER 트랜잭션이 커밋된 **뒤**(선점 쿼리가 방금 쓴 행을 보도록).
        # 큐는 FIFO 라 방금 넣은 REGISTER_ACK 다음에 나간다.
        if (kind is TopicKind.REGISTER and self._retrier is not None
                and msg_type == MsgType.REGISTER.value):
            await self._retrier.on_device_messages([uuid], reason="register")

    # ── REGISTER ────────────────────────────────────────────────────────
    async def handle_register(
        self, db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        if str(data.get("type") or "") != MsgType.REGISTER.value:
            metrics.unknown_type += 1
            log.warning("register 토픽에 type=%r (%s)", data.get("type"), uuid)
            return

        values: dict[str, Any] = {"last_register_at": now}
        for key in ("fw", "device_model", "modem_model", "imei", "iccid", "msisdn"):
            if key in data:
                values[key] = _text(data[key])
        for key, column in (("cv", "cv_device"), ("ss", "ss_device"),
                            ("ti", "ti_device"), ("ka", "ka_device")):
            if key in data and _int(data[key]) is not None:
                values[column] = _int(data[key])
        await _touch_device(db, uuid, seen_at=now, **values)
        # REGISTER 는 재연결마다 같은 내용으로 오는 것이 정상이라 dedup 키를 두지 않는다.
        await _insert_event(
            db, uuid=uuid, kind=EventKind.REGISTER, payload=data, key=None, received_at=now
        )
        # REGISTER 는 살아 있는 연결로만 올 수 있다. 브로커 로그 tail 이 꺼져 있거나(개발 PC)
        # 재시작으로 줄을 놓쳤어도 여기서 online 이 맞춰진다. 플래그가 이미 true 면 no-op.
        await presence.apply_presence(db, {uuid: True}, now, source="register")

        row = (await db.execute(
            select(*CONFIG_COLUMNS, Device.site, Device.state_reason, Device.grp)
            .where(Device.uuid == uuid)
        )).first()
        if row is None:  # 방금 upsert 했으니 없을 수 없다
            return
        cfg = DeviceConfigRow(*row[:len(CONFIG_COLUMNS)])
        site, state_reason, grp = row[len(CONFIG_COLUMNS):len(CONFIG_COLUMNS) + 3]

        # RETIRED 단말이 다시 REGISTER 를 보냈다 = 같은 보드를 다른 곳에 재설치했다(docs/05
        # 상태 전이 표). 빈 retain 상태라 승인 절차를 처음부터 다시 밟는다 → PENDING.
        state = cfg.state
        if state == DeviceState.RETIRED.value:
            await db.execute(
                update(Device).where(Device.uuid == uuid).values(
                    state=DeviceState.PENDING.value, state_reason=None, state_changed_at=now,
                )
            )
            await _insert_event(
                db, uuid=uuid, kind=EventKind.STATE_CHANGE, key=None, received_at=now,
                payload={"from": state, "to": DeviceState.PENDING.value, "by": "register"},
            )
            log.info("RETIRED 단말 재등록 → PENDING: %s", uuid)
            state, state_reason = DeviceState.PENDING.value, None

        if self._config_sync is None:
            return
        # 1. REGISTER_ACK 는 상태와 무관하게 **항상**, 즉시 (사양서 §3.3, S-7).
        self._config_sync.offer_register_ack(
            register_ack_job_for(uuid=uuid, state=state, site=site, reason=state_reason, grp=grp)
        )
        # 2. ACTIVE 일 때만 cv 비교 → CONFIG_SET (S-10, S-13). FIFO 라 ACK 뒤에 나간다.
        await decide_and_enqueue(
            db, self._config_sync, [dataclasses.replace(cfg, state=state)], reason="register"
        )

    # ── result ──────────────────────────────────────────────────────────
    async def handle_result(
        self, db: AsyncSession, uuid: str, msg_type: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        # 결과가 왔다는 건 살아 있다는 뜻이다. 미등록 uuid 면 여기서도 등록된다(PENDING).
        await _touch_device(db, uuid, seen_at=now)

        if msg_type == MsgType.PONG.value:
            seq = _int(data.get("seq"))
            inserted = await _insert_event(
                db, uuid=uuid, kind=EventKind.PONG, payload=data,
                key=dedup_key(uuid, "PONG", seq, data), received_at=now,
            )
            if inserted and seq is not None:
                await self._ack_command(db, uuid, seq, "OK", data, now)
            return

        if msg_type == MsgType.CONFIG_ACK.value:
            await self._handle_config_ack(db, uuid, data, now)
            return

        if msg_type in COMMAND_ACK_TYPES:
            await self._handle_command_ack(db, uuid, data, now)
            return

        metrics.unknown_type += 1
        log.warning("result 토픽에 알 수 없는 type=%r (%s)", msg_type, uuid)

    async def _handle_config_ack(
        self, db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        cv = _int(data.get("cv"))
        result = str(data.get("result") or "")
        await _insert_event(
            db, uuid=uuid, kind=EventKind.CONFIG_ACK, payload=data,
            key=dedup_key(uuid, "CONFIG_ACK", cv, data), received_at=now,
        )
        if result == "OK":
            metrics.config_ack_ok += 1
            if cv is None:
                return
            # 단말이 "전부 적용하고 Flash 에 저장" 했다(§1.1.7). ti/ka 는 TELEMETRY 에 실리지
            # 않아 다음 REGISTER(재부팅) 전까지 알 길이 없으므로 여기서 서버 적용값을 적는다.
            # 단, ack 의 cv 가 지금 서버 cv 와 같을 때만 — 늦게 온 옛 ack 로 덮지 않는다.
            # cv_device 는 다음 TELEMETRY echo 가 어차피 다시 쓴다.
            await db.execute(
                update(Device)
                .where(Device.uuid == uuid, Device.cv_server == cv)
                .values(cv_device=cv, ti_device=effective_ti_sql(), ka_device=effective_ka_sql(),
                        updated_at=func.now())
            )
            return
        if result == "RANGE":
            # 단말이 거부했다 = 서버가 범위 밖 값을 보냈다. PATCH/프로필 검증이 막았어야 한다.
            metrics.config_ack_range += 1
            log.warning("CONFIG_ACK RANGE %s cv=%s — 서버가 보낸 값이 범위 밖", uuid, cv)
            return
        if result == "STATE":
            # 단말은 자기가 승인 전이라고 한다. DB 가 ACTIVE 면 단말이 REGISTER_ACK 를 못 받은
            # 것(retain 유실·발행 실패)이므로 다시 retain 한다. 다른 상태면 서버가 ACTIVE 전에
            # CONFIG 를 보낸 것이라 버그다 — 판정이 막았어야 한다.
            metrics.config_ack_state += 1
            row = (await db.execute(
                select(Device.state, Device.site, Device.state_reason, Device.grp)
                .where(Device.uuid == uuid)
            )).first()
            if row is not None and row[0] == DeviceState.ACTIVE.value:
                log.warning("CONFIG_ACK STATE %s 인데 DB 는 ACTIVE — REGISTER_ACK 재발행", uuid)
                if self._config_sync is not None:
                    self._config_sync.offer_register_ack(register_ack_job_for(
                        uuid=uuid, state=row[0], site=row[1], reason=row[2], grp=row[3]
                    ))
            else:
                log.error("CONFIG_ACK STATE %s (DB state=%s) — ACTIVE 전에 CONFIG 가 나갔다",
                          uuid, row[0] if row else None)
            return
        if result == "FLASH":
            # Flash 기록 실패, 단말은 이전 값 유지. 같은 CONFIG 를 다시 보내도 된다(§1.1.7) —
            # 쿨다운을 풀어 다음 송신 때 바로 재전송한다. 반복되면 단말 점검(카운터로 본다).
            metrics.config_ack_flash += 1
            log.warning("CONFIG_ACK FLASH %s cv=%s — 다음 송신 때 재전송", uuid, cv)
            if self._config_sync is not None:
                self._config_sync.clear_cooldown(uuid)
            return
        log.warning("CONFIG_ACK 알 수 없는 result=%r %s cv=%s", result, uuid, cv)

    async def _handle_command_ack(
        self, db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        """COMMAND_ACK (사양서 §3.10.11, ADR-005).

        1. device_event(COMMAND_ACK) — dedup 키로 QoS1 재전송을 거른다(걸리면 여기서 끝).
        2. command 가 COMMAND 이고 이 단말이 **대상 스냅숏에 있어야** 한다(§1.1.5 seq·uuid 대조).
           모르는 seq·스냅숏 밖 단말은 경고 + command_ack_mismatch.
        3. command_target: status(= result, OK 는 안 내려감), acked_at, ack. 첫 응답이면
           command.acked_count + 1.
        4. OK 면 device.override_* (until = 그 대상에 마지막으로 보낸 ts + dur).
        5. 대상이 다 응답했으면 종료 판정(finish_due)을 이 seq 하나에 바로 돌린다.
        """
        seq = _int(data.get("seq"))
        result = str(data.get("result") or "").strip().upper()
        inserted = await _insert_event(
            db, uuid=uuid, kind=EventKind.COMMAND_ACK, payload=data,
            key=dedup_key(uuid, "COMMAND_ACK", seq, data), received_at=now,
        )
        if not inserted or seq is None:
            return
        metrics.command_ack += 1

        cmd = (await db.execute(
            select(Command.type, Command.target_kind, Command.payload, Command.sent_at,
                   Command.expected_count, Command.acked_count)
            .where(Command.seq == seq)
        )).first()
        if cmd is None or cmd[0] != MsgType.COMMAND.value:
            metrics.command_ack_mismatch += 1
            log.warning("모르는 seq 의 COMMAND_ACK %s seq=%s (지운 명령이거나 위조)", uuid, seq)
            return
        target = (await db.execute(
            select(CommandTarget).where(CommandTarget.seq == seq, CommandTarget.uuid == uuid)
            .with_for_update()
        )).scalar_one_or_none()
        if target is None:
            metrics.command_ack_mismatch += 1
            log.warning("seq=%s 대상 스냅숏에 없는 단말의 COMMAND_ACK %s (보낸 뒤 그룹에 들어옴?)",
                        seq, uuid)
            return
        if result not in ACK_RESULTS:
            # 상태는 두고 원본만 남긴다 — 모르는 값으로 집계를 흐리지 않는다.
            log.warning("COMMAND_ACK 알 수 없는 result=%r %s seq=%s", result, uuid, seq)
            target.ack = data
            return

        first = target.status == TargetStatus.PENDING.value
        target.status = next_target_status(target.status, result)
        target.acked_at = now
        target.ack = data
        if first:
            await db.execute(
                update(Command).where(Command.seq == seq)
                .values(acked_count=Command.acked_count + 1)
            )

        if result == TargetStatus.OK.value:
            await self._apply_override(db, uuid, seq, cmd[1], cmd[2] or {}, target, now)

        if first and (cmd[5] or 0) + 1 >= (cmd[4] or 0):
            await db.flush()
            await finish_due(db, now, seqs=[seq])

    async def _apply_override(
        self, db: AsyncSession, uuid: str, seq: int, target_kind: str,
        payload: dict[str, Any], target: CommandTarget, now: dt.datetime,
    ) -> None:
        """OK 응답 → device.override_* (§3.10.8). until 기준은 그 단말이 적용한 발송의 ts —
        첫 발송이면 payload.ts, 재시도였으면 last_sent_at(재발송 ts 와 같은 초)."""
        sent_at = target.last_sent_at
        if sent_at is None:
            try:
                sent_at = parse_kst_ts(str(payload.get("ts")))
            except ValueError:
                sent_at = now
        device = (await db.execute(
            select(Device).where(Device.uuid == uuid).with_for_update()
        )).scalar_one_or_none()
        if device is None:
            return
        current = OverrideState(device.override_act, device.override_level,
                                device.override_seq, device.override_until)
        dur = payload.get("dur")
        new = override_after_ok(
            current, act=str(payload.get("act") or ""), level=override_level_for(target_kind),
            seq=seq, sent_at=sent_at, dur=int(dur) if isinstance(dur, int) else None, now=now,
        )
        if new is None:
            return
        device.override_act = new.act
        device.override_level = new.level
        device.override_seq = new.seq
        device.override_until = new.until

    async def _ack_command(
        self, db: AsyncSession, uuid: str, seq: int, result: str, data: dict[str, Any],
        now: dt.datetime,
    ) -> None:
        """command_ack 1행 + command 집계. 사양서 §1.1.5: topic·uuid·seq 가 보낸 것과 일치해야."""
        cmd = (
            await db.execute(
                select(Command.target_kind, Command.target_id, Command.expected_count)
                .where(Command.seq == seq)
            )
        ).first()
        if cmd is None:
            metrics.pong_mismatch += 1
            log.warning("모르는 seq 응답 %s seq=%s (재시작 전 명령이거나 위조)", uuid, seq)
            return
        if cmd[0] == "device" and cmd[1] != uuid:
            metrics.pong_mismatch += 1
            log.warning("seq=%s 응답 단말 불일치: 보낸 곳 %s, 응답 %s", seq, cmd[1], uuid)
            return

        ack = await db.execute(
            pg_insert(CommandAck)
            .values(seq=seq, uuid=uuid, result=result, payload=data, received_at=now)
            .on_conflict_do_nothing()
        )
        if not ack.rowcount:
            return
        expected = cmd[2] or 1
        # 두 단계로 쓴다: 카운트 증가 → 다 모였으면 종료 확정. 그룹 명령(5차)의 PARTIAL/
        # TIMEOUT 판정은 별도 타이머가 하고, 여기서는 "전부 응답" 만 OK 로 닫는다.
        await db.execute(
            update(Command)
            .where(Command.seq == seq)
            .values(acked_count=Command.acked_count + 1)
        )
        await db.execute(
            update(Command)
            .where(Command.seq == seq, Command.acked_count >= expected)
            .values(finished_at=func.coalesce(Command.finished_at, now), result="OK")
        )

    # ── event ───────────────────────────────────────────────────────────
    async def handle_event(
        self, db: AsyncSession, uuid: str, msg_type: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        if msg_type == MsgType.LWT.value:
            # 모뎀은 Will 을 못 넣지만 시뮬레이터·후속 모뎀은 넣을 수 있다(ADR-004). 브로커 로그
            # 경로와 같은 apply_presence 로 처리한다 — 플래그가 바뀔 때만 OFFLINE 이력.
            # 버퍼에 남은 그 단말의 TM 은 버리지 않는다(이력이라 한 건도 아깝다). TM flush 는
            # `online` 을 건드리지 않으므로 늦게 적재된 TM 이 단말을 되살리지 않는다(S2-13).
            # seen_at 갱신 없음: LWT 는 단말이 아니라 브로커가 보낸다.
            await presence.apply_presence(db, {uuid: False}, now, source="lwt")
            await _insert_event(
                db, uuid=uuid, kind=EventKind.LWT, payload=data, key=None, received_at=now
            )
            log.info("LWT %s → offline", uuid)
            return

        if msg_type == MsgType.EV.value:
            await _touch_device(db, uuid, seen_at=now)
            # ev=ERR 만 정의돼 있다(§16.2.1). 다른 ev 값도 ERR 로 쌓되 payload 로 구분한다.
            await _insert_event(
                db, uuid=uuid, kind=EventKind.ERR, payload=data,
                key=dedup_key(uuid, "EV", data.get("ts"), data), received_at=now,
            )
            return

        metrics.unknown_type += 1
        log.warning("event 토픽에 알 수 없는 type=%r (%s)", msg_type, uuid)
