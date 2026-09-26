"""단말 온라인 판정 — 한 곳에서만 정의한다 (ADR-004, 사양서 §16.1, docs/05 DeviceOut).

판정이 두 벌이면 화면마다 다른 말을 한다(목록은 온라인인데 상세는 오프라인).
파이썬 판(is_online)과 SQL 판(online_clause)이 **반드시 같은 규칙**이어야 하므로
둘을 나란히 둔다.

규칙 (A + C, 둘 다 만족해야 true):
    is_online = device.online                                   (A: 브로커 로그 / LWT)
            AND last_seen_at >= now - window                    (C: 수신 시각 보조)
    window   = ACTIVE   → effective_ti × DEVICE_ONLINE_FACTOR   (ti 는 override ?? profile)
               그 외    → PENDING_OFFLINE_SEC (70분 — REGISTER 재전송 최대 30분 × 2 + 여유)

C 를 같이 두는 이유: 백엔드 재시작으로 tail 위치를 잃거나 로그 회전에 줄이 빠지면 `online`
플래그가 굳는다. 수신이 끊긴 지 오래면 플래그와 무관하게 오프라인으로 본다.
ACTIVE 의 창에 단말 보고값(ti_device)이 아니라 서버 적용값을 쓰는 이유: 단말이 아직
옛 주기로 보내는 동안은 cv 불일치로 CONFIG 가 곧 나가고, 창을 3배로 잡아 흡수된다.

플래그 갱신(apply_presence)은 브로커 로그 tail 과 LWT 가 같이 쓴다 — 플래그가 바뀔 때만
online_changed_at 과 device_event(ONLINE/OFFLINE) 를 남긴다(같은 줄이 두 번 와도 이력이
두 번 남지 않는다).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import and_, case, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import DeviceState, EventKind
from app.core.effective import effective_ti_sql
from app.models.device import Device
from app.models.event import DeviceEvent


class _PresenceFields(Protocol):
    online: bool
    state: str
    last_seen_at: dt.datetime | None


def online_window_sec(
    state: str, effective_ti: int, *, factor: int | None = None, pending_sec: int | None = None
) -> int:
    factor = settings.device_online_factor if factor is None else factor
    pending_sec = settings.pending_offline_sec if pending_sec is None else pending_sec
    if state == DeviceState.ACTIVE.value:
        return int(effective_ti * factor)
    return int(pending_sec)


def is_online(
    device: _PresenceFields, now: dt.datetime, effective_ti: int, *,
    factor: int | None = None, pending_sec: int | None = None,
) -> bool:
    if not device.online:
        return False
    last = device.last_seen_at
    if last is None:
        return False
    window = online_window_sec(device.state, effective_ti, factor=factor, pending_sec=pending_sec)
    return last >= now - dt.timedelta(seconds=window)


def online_clause(now: dt.datetime):
    """is_online() 의 SQL 판. 규칙이 바뀌면 둘을 같이 고친다."""
    window = case(
        (Device.state == DeviceState.ACTIVE.value,
         effective_ti_sql() * settings.device_online_factor),
        else_=settings.pending_offline_sec,
    )
    cutoff = now - window * text("interval '1 second'")
    return and_(
        Device.online.is_(True),
        Device.last_seen_at.is_not(None),
        Device.last_seen_at >= cutoff,
    )


# ── 플래그 갱신 (브로커 로그 tail · LWT 공용) ─────────────────────────────
def collapse_transitions(items: Iterable[tuple[str, bool]]) -> dict[str, bool]:
    """같은 poll 묶음 안에서 같은 UUID 가 여러 번 나오면 **마지막** 것만 남긴다.
    (접속→끊김→접속 이 한 묶음에 있으면 결과는 접속.) 순서를 유지한 dict."""
    out: dict[str, bool] = {}
    for uuid, online in items:
        out.pop(uuid, None)
        out[uuid] = online
    return out


async def apply_presence(
    db: AsyncSession, transitions: dict[str, bool], now: dt.datetime, *, source: str
) -> tuple[int, int]:
    """online 플래그 반영. 반환 (ONLINE 전이 수, OFFLINE 전이 수) — 플래그가 실제로 바뀐 것만.

    처음 보는 UUID 는 행을 만들지 않는다 — 접속만 하고 REGISTER 를 안 보낸 단말은 승인 대상이
    아니다(사양서 §3.2 "REGISTER 가 먼저"). last_seen_at 은 건드리지 않는다(브로커가 본 것이지
    단말이 보낸 메시지가 아니다).
    """
    if not transitions:
        return 0, 0
    counts = {True: 0, False: 0}
    for online in (True, False):
        group = [u for u, o in transitions.items() if o is online]
        if not group:
            continue
        values: dict[str, object] = {
            "online": online, "online_changed_at": now, "updated_at": now,
        }
        if not online:
            values["offline_at"] = now
        result = await db.execute(
            update(Device)
            .where(Device.uuid.in_(group), Device.online.is_not(online))
            .values(**values)
            .returning(Device.uuid)
        )
        changed = [row[0] for row in result.all()]
        if not changed:
            continue
        kind = EventKind.ONLINE if online else EventKind.OFFLINE
        await db.execute(
            pg_insert(DeviceEvent).values([
                {"uuid": uuid, "kind": kind.value, "payload": {"source": source},
                 "received_at": now}
                for uuid in changed
            ])
        )
        counts[online] = len(changed)
    return counts[True], counts[False]
