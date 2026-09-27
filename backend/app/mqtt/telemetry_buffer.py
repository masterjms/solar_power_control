"""Telemetry 수신 버퍼 — 1초에 한 번, 두 문장으로 쓴다.

왜 필요한가:
    메시지마다 트랜잭션을 열면 DB 왕복 지연이 처리량 상한이 된다. 1만 대가 10분 주기면
    평균 17 msg/s 지만 브로커 재시작 뒤에는 REGISTER 직후 TM 이 한꺼번에 온다. aircast
    실측: 건별 152 msg/s → 배치 11,928 msg/s.

무엇을 하나 (flush 1회):
    1. telemetry 이력   — 모인 행 전부 multi-row INSERT (1,000행씩 끊어서. asyncpg 는
       문장당 바인드 32,767개 한계가 있고 컬럼이 21개다)
    2. device 최신값    — uuid 별 마지막 1건만 multi-row upsert. last_seen_at /
       last_telemetry_at 은 GREATEST — 버퍼가 늦게 flush 돼도 시각이 뒤로 가지 않는다
       (result 메시지가 그 사이 더 최근 값을 썼을 수 있다). lost/reboot 증가분은
       `lost_count = lost_count + EXCLUDED.lost_count` 로 누적한다
    3. sq 판정         — 프로세스 내 last_sq 캐시(기동 때 DB 에서 적재)로 판정. DB 를
       읽지 않는다. REBOOT/LOST 는 device_event 행으로 남긴다
    4. CONFIG 재전송   — RETURNING 으로 받은 state/cv_server/프로필 로 판정(config_decide).
       ACTIVE 이고 cv 가 다르면 cv_server 확정 후 ConfigSyncQueue 에 넣는다
       (사양서 §1.1.10 "단말 송신 직후", ADR-002, 60초 쿨다운)
    5. ACTIVE 가 아닌 단말의 TM 은 저장은 하되 `telemetry_not_active` 로 센다 — 사양서 §3.8
       상 보내면 안 되는 상태다(승인 전 펌웨어 1.1.x 이거나 retain 이 어긋난 것)
    6. (5차) 재부팅 감지 단말, 그리고 **md == 1(현장 조작) TM 을 보낸 단말**은 override 표시 필드를
       지운다 — 재부팅은 슬롯을 잃고, 현장 조작 시작은 원격을 전부 취소한다
       (§3.10.8 2026-09-27 개정).
       대기 중인 command_target 은 건드리지 않는다(단말이 LOCAL 로 답하거나 무응답으로 끝난다).
       커밋 뒤 이 묶음의 uuid 로 COMMAND 자동 재시도·S-23 설정 재발송을 부른다(§1.1.10)
    7. (S-23) Telemetry `ss` 가 device_settings.ss_known 과 다르고 sync 가 synced 이면 local_saved
       (현장에서 저장함, UI_항목_명세 8.5). device 조인 UPDATE 한 문장 — 이 묶음 uuid 만 본다.
       ACK/SETTINGS 가 기준을 갱신한 **뒤에** 받은 TM 만 비교한다(그 전 TM 은 옛 ss 가 정상).

원격 OK·유지시간 끝·현장 시작 뒤 2초에 오는 추가 TM(§3.10.8)은 주기 TM 과 같다 — sq 가 이어지므로
유실·재부팅으로 보지 않고, 주기보다 이르다고 따로 판정하는 곳도 없다.

안전장치:
    · flush 실패 → 그 묶음은 **버린다**. 다시 큐에 넣지 않는다. DB 가 아픈 동안 대기열이
      무한정 자라는 것이 재기동보다 나쁘다(docs/00 §5). telemetry_dropped 로 센다.
      last_sq 캐시는 이미 올라가 있으므로 다음 묶음은 그 묶음이 "유실"로 보이지 않는다 —
      의도한 것이다. 버린 행은 이력에서 빠지지만 카운터가 그 사실을 남긴다.
    · 대기 행이 TELEMETRY_FLUSH_MAX_PENDING 을 넘으면 주기를 기다리지 않는다.
    · 종료 시 남은 것을 flush 한다.
    · 처음 보는 uuid 는 여기서 device 행이 생긴다(PENDING, 사양서 §4.1 "REGISTER 를 놓쳐도").
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import DeviceState, EventKind
from app.core.metrics import metrics
from app.core.settings_rules import SYNC_LOCAL_SAVED, SYNC_SYNCED
from app.db import session_scope
from app.models.command import CommandTarget
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.settings import DeviceSettings
from app.models.telemetry import Telemetry
from app.mqtt import sq as sq_rules
from app.mqtt.config_decide import (
    CONFIG_COLUMNS,
    DeviceConfigRow,
    decide_and_enqueue,
    load_profiles,
)
from app.mqtt.config_sync import ConfigSyncQueue

if TYPE_CHECKING:
    from app.mqtt.command_retry import CommandRetrier
    from app.mqtt.settings_sync import SettingsSync

log = logging.getLogger(__name__)

#: 재부팅 감지 시 지우는 override 표시 필드(§3.10.8 "재부팅 → 전부 소거").
OVERRIDE_CLEAR = {
    "override_act": None, "override_level": None, "override_seq": None, "override_until": None,
}

#: 문장당 행 수. 21컬럼 × 1000 = 21,000 바인드 < 32,767.
_CHUNK = 1000


def _int(value: Any) -> int | None:
    """숫자로 읽을 수 있으면 int, 아니면 None. 단말이 문자열로 보내도 죽지 않는다."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def telemetry_row(uuid: str, payload: dict[str, Any], received_at: dt.datetime) -> dict[str, Any]:
    """TELEMETRY payload → telemetry 컬럼. 단위 변환 없음, 원본은 raw 에 통째로."""
    pw = payload.get("pw")
    if not isinstance(pw, list):
        pw = []
    pw = list(pw) + [None, None, None]
    return {
        "uuid": uuid,
        "received_at": received_at,
        "ts_device": str(payload["ts"]) if payload.get("ts") is not None else None,
        "sq": _int(payload.get("sq")),
        "fw": str(payload["fw"]) if payload.get("fw") is not None else None,
        "ss": _int(payload.get("ss")),
        "cv": _int(payload.get("cv")),
        "er": _int(payload.get("er")),
        "on": _int(payload.get("on")),
        "md": _int(payload.get("md")),
        "pw1": _int(pw[0]),
        "pw2": _int(pw[1]),
        "pw3": _int(pw[2]),
        "bv": _int(payload.get("bv")),
        "bi": _int(payload.get("bi")),
        "sc": _int(payload.get("sc")),
        "pp": _int(payload.get("pp")),
        "li": _int(payload.get("li")),
        "cs": _int(payload.get("cs")),
        "raw": payload,
    }


def override_clear_times(
    batch: list[tuple[str, dict[str, Any], dt.datetime]], events: list[dict[str, Any]]
) -> dict[str, dt.datetime]:
    """override 표시를 지울 단말 → 기준 시각(그 단말의 마지막 REBOOT 또는 md == 1 TM 수신 시각).

    md == 1(현장 조작): 단말이 살아 있던 원격을 전부 취소했다(§3.10.8 2026-09-27 개정). md 가 2 로
    돌아온 TM 이 같은 묶음 뒤쪽에 있어도 기준 시각 이후에 보낸 명령의 override 는 호출부가
    남긴다."""
    out: dict[str, dt.datetime] = {}
    for e in events:
        if e["kind"] == EventKind.REBOOT.value:
            out[e["uuid"]] = max(out.get(e["uuid"], e["received_at"]), e["received_at"])
    for uuid, payload, at in batch:
        if _int(payload.get("md")) == 1:
            out[uuid] = max(out.get(uuid, at), at)
    return out


class TelemetryBuffer:
    def __init__(
        self,
        *,
        interval_sec: float | None = None,
        max_pending: int | None = None,
        config_sync: ConfigSyncQueue | None = None,
        retrier: CommandRetrier | None = None,
        settings_sync: SettingsSync | None = None,
    ) -> None:
        self._interval = interval_sec if interval_sec is not None else settings.flush_interval
        self._max_pending = max_pending or settings.telemetry_flush_max_pending
        self._config_sync = config_sync
        #: 5차 COMMAND 자동 재시도(단말 송신 직후). flush 커밋 뒤 이 묶음의 uuid 로 부른다.
        self._retrier = retrier
        #: S-23 설정 요청 재발송(단말 송신 직후). flush 커밋 뒤 부른다.
        self._settings_sync = settings_sync
        #: 이력 후보. (uuid, payload, received_at) 도착 순서대로.
        self._rows: list[tuple[str, dict[str, Any], dt.datetime]] = []
        #: uuid → 마지막 last_sq. 기동 시 warm() 으로 채운다.
        self._last_sq: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ── 적재 ────────────────────────────────────────────────────────────
    def offer(self, uuid: str, *, payload: dict[str, Any], received_at: dt.datetime) -> None:
        """TM 한 건을 대기열에 넣는다. DB 를 만지지 않는다."""
        self._rows.append((uuid, payload, received_at))
        if len(self._rows) >= self._max_pending:
            self._wake.set()

    def discard(self, uuid: str) -> int:
        """대기 중인 그 단말의 TM 을 버린다."""
        before = len(self._rows)
        self._rows = [row for row in self._rows if row[0] != uuid]
        return before - len(self._rows)

    @property
    def pending_count(self) -> int:
        return len(self._rows)

    def last_sq_of(self, uuid: str) -> int | None:
        return self._last_sq.get(uuid)

    # ── 수명주기 ────────────────────────────────────────────────────────
    async def warm(self, db: AsyncSession) -> int:
        """DB 의 last_sq 로 캐시를 채운다. 재기동 직후 첫 TM 이 전부 '처음 보는 단말'로
        판정되어 유실·재부팅을 놓치는 일을 막는다."""
        rows = await db.execute(
            select(Device.uuid, Device.last_sq).where(Device.last_sq.is_not(None))
        )
        self._last_sq = {uuid: int(last_sq) for uuid, last_sq in rows}
        return len(self._last_sq)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="telemetry-buffer")

    async def stop(self) -> None:
        """주기 태스크를 접고 남은 것을 마지막으로 쓴다."""
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 - 종료 경로에서 예외를 올리지 않는다
            log.exception("종료 시 telemetry flush 실패 (마지막 1초분 유실)")

    async def _run(self) -> None:
        while not self._stopping:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            self._wake.clear()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 한 번 실패해도 루프는 계속 돈다
                log.exception("telemetry flush 실패")

    # ── 판정 (순수, DB 없음) ────────────────────────────────────────────
    def _judge_batch(
        self, batch: list[tuple[str, dict[str, Any], dt.datetime]]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """묶음을 훑으며 uuid 별 최신값 행과 REBOOT/LOST 이벤트 행을 만든다.

        last_sq 캐시를 여기서 갱신한다 — flush 가 실패해도 되돌리지 않는다(모듈 docstring).
        """
        latest: dict[str, dict[str, Any]] = {}
        events: list[dict[str, Any]] = []
        for uuid, payload, received_at in batch:
            entry = latest.setdefault(uuid, {"lost": 0, "reboot": 0})
            sq_value = _int(payload.get("sq"))
            if sq_value is not None:
                verdict = sq_rules.judge(self._last_sq.get(uuid), sq_value)
                if verdict.lost:
                    entry["lost"] += verdict.lost
                    events.append({
                        "uuid": uuid, "kind": EventKind.LOST.value,
                        "payload": {"sq": sq_value, "last_sq": self._last_sq.get(uuid),
                                    "lost": verdict.lost, "jump": verdict.jump},
                        "received_at": received_at,
                    })
                if verdict.reboot:
                    entry["reboot"] += 1
                    events.append({
                        "uuid": uuid, "kind": EventKind.REBOOT.value,
                        "payload": {"sq": sq_value, "last_sq": self._last_sq.get(uuid)},
                        "received_at": received_at,
                    })
                self._last_sq[uuid] = sq_value
            entry["payload"] = payload
            entry["received_at"] = received_at
            entry["sq"] = sq_value
        return latest, events

    # ── 쓰기 ────────────────────────────────────────────────────────────
    async def flush(self) -> int:
        """대기분을 한 트랜잭션에 쓰고, cv 불일치 ACTIVE 단말을 CONFIG 큐에 넣는다. 반환: 행 수."""
        if not self._rows:
            return 0
        batch = self._rows
        self._rows = []
        started = time.perf_counter()

        latest, events = self._judge_batch(batch)
        history = [telemetry_row(uuid, payload, at) for uuid, payload, at in batch]
        device_rows = [
            {
                "uuid": uuid,
                "last_telemetry": e["payload"],
                "last_telemetry_at": e["received_at"],
                "last_seen_at": e["received_at"],
                "last_sq": e["sq"],
                "cv_device": _int(e["payload"].get("cv")),
                "ss_device": _int(e["payload"].get("ss")),
                "fw": str(e["payload"]["fw"]) if e["payload"].get("fw") is not None else None,
                "lost_count": e["lost"],
                "reboot_count": e["reboot"],
            }
            for uuid, e in latest.items()
        ]

        not_active = 0
        local_saved = 0
        try:
            async with session_scope() as db:
                for i in range(0, len(history), _CHUNK):
                    await db.execute(
                        pg_insert(Telemetry).values(history[i:i + _CHUNK]).on_conflict_do_nothing()
                    )
                profiles = await load_profiles(db)
                for i in range(0, len(device_rows), _CHUNK):
                    stmt = pg_insert(Device).values(device_rows[i:i + _CHUNK])
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[Device.uuid],
                        set_={
                            "last_telemetry": stmt.excluded.last_telemetry,
                            "last_telemetry_at": func.greatest(
                                Device.last_telemetry_at, stmt.excluded.last_telemetry_at
                            ),
                            "last_seen_at": func.greatest(
                                Device.last_seen_at, stmt.excluded.last_seen_at
                            ),
                            "last_sq": stmt.excluded.last_sq,
                            "cv_device": stmt.excluded.cv_device,
                            "ss_device": stmt.excluded.ss_device,
                            # fw 는 TM 마다 오지만 None 이면 기존 값을 유지한다.
                            "fw": func.coalesce(stmt.excluded.fw, Device.fw),
                            "lost_count": Device.lost_count + stmt.excluded.lost_count,
                            "reboot_count": Device.reboot_count + stmt.excluded.reboot_count,
                            "updated_at": func.now(),
                        },
                    ).returning(*CONFIG_COLUMNS)
                    rows = [DeviceConfigRow(*r) for r in (await db.execute(stmt)).all()]
                    not_active += sum(1 for r in rows if r.state != DeviceState.ACTIVE.value)
                    # cv_device 는 방금 쓴 payload 값이다(RETURNING 은 갱신 후 행을 준다).
                    await decide_and_enqueue(
                        db, self._config_sync, rows, reason="telemetry", profiles=profiles
                    )
                if events:
                    await db.execute(pg_insert(DeviceEvent).values(events))
                # 5차: 재부팅한 단말은 override 슬롯을 전부 잃고 스케줄로 시작한다(§3.10.7), 현장
                # 조작(md == 1)이 시작되면 단말이 원격을 전부 취소한다(§3.10.8 개정) — 화면의
                # "원격 n분 남음"도 같이 지운다.
                # 단, 그 TM 을 받은 **뒤에** 보낸 명령으로 기록된 override 는 지우지 않는다.
                # flush 는 최대 1초 늦게 돌아서, 그 사이 새 명령의 ACK 가 먼저 기록될 수 있다
                # (시나리오 B11 에서 실제로 지워졌다). 드물어서 단말별로 본다.
                for uuid, at in sorted(override_clear_times(batch, events).items()):
                    sent_at = (
                        select(CommandTarget.last_sent_at)
                        .where(CommandTarget.seq == Device.override_seq,
                               CommandTarget.uuid == Device.uuid)
                        .correlate(Device)
                        .scalar_subquery()
                    )
                    await db.execute(
                        update(Device)
                        .where(Device.uuid == uuid,
                               Device.override_seq.is_not(None),
                               func.coalesce(sent_at, at) <= at)
                        .values(**OVERRIDE_CLEAR)
                    )
                # S-23: 현장 저장 감지(ss ≠ ss_known). 방금 upsert 한 device.ss_device 와 조인.
                ss_uuids = [u for u, e in latest.items()
                            if _int(e["payload"].get("ss")) is not None]
                for i in range(0, len(ss_uuids), _CHUNK):
                    res = await db.execute(
                        update(DeviceSettings)
                        .where(
                            DeviceSettings.uuid == Device.uuid,
                            Device.uuid.in_(ss_uuids[i:i + _CHUNK]),
                            DeviceSettings.sync == SYNC_SYNCED,
                            DeviceSettings.ss_known.is_not(None),
                            Device.ss_device != DeviceSettings.ss_known,
                            Device.last_telemetry_at > DeviceSettings.updated_at,
                        )
                        .values(sync=SYNC_LOCAL_SAVED, updated_at=func.now())
                        .execution_options(synchronize_session=False)
                    )
                    local_saved += res.rowcount or 0
        except Exception:  # noqa: BLE001
            metrics.telemetry_dropped += len(history)
            metrics.flush_failures += 1
            log.exception("telemetry flush 실패 — %d행 폐기 (단말 %d대)", len(history), len(latest))
            return 0

        metrics.telemetry_flushed += len(history)
        metrics.record_flush((time.perf_counter() - started) * 1000.0)
        if local_saved:
            metrics.settings_local_saved += local_saved
            log.info("Telemetry ss 변화 — 현장 저장(local_saved) %d대", local_saved)
        if self._retrier is not None:
            # 커밋 뒤. 후보 집합에 없는 uuid 는 쿼리 없이 걸러진다(평소 비용 0).
            await self._retrier.on_device_messages(latest.keys(), reason="telemetry")
        if self._settings_sync is not None:
            await self._settings_sync.on_device_messages(latest.keys(), reason="telemetry")
        if not_active:
            metrics.telemetry_not_active += not_active
            log.debug("ACTIVE 아닌 단말의 TELEMETRY %d대 (저장은 함)", not_active)
        return len(history)
